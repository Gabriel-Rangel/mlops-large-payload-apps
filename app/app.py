"""Databricks App como porta de entrada do Credit Engine.

Fluxo de cada request (até MAX_BODY_BYTES, padrão 64 MiB):
  1. recebe o POST completo (15 variáveis + `payload` com o histórico do birô);
  2. grava o corpo original como request.json no Volume ANTES de responder (202 durável);
  3. responde 202 com o request_id;
  4. em segundo plano: pontua o modelo @Champion no próprio processo e grava response_dbx.json
     ao lado do request.json; com o Volume EXTERNAL no bucket do cliente, esse arquivo dispara
     o evento S3 → SNS → response router que já existe;
  5. por último, grava uma linha de metadados em inference_observability.

GET /invocations/{id} responde a partir da memória ou, se esta instância não conhece o id
(restart, várias instâncias), a partir dos arquivos no Volume.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import job_store
import storage
from scoring import ScorerHolder, extract_scalars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("large-payload-app")

# As Databricks Apps injetam o host sem o esquema.
_host = os.environ.get("DATABRICKS_HOST", "")
if _host and not _host.startswith("http"):
    os.environ["DATABRICKS_HOST"] = f"https://{_host}"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Variável de ambiente {name} é obrigatória (definida em resources/app.yml)")
    return value


CATALOG = _required_env("DATABRICKS_CATALOG")
SCHEMA = _required_env("DATABRICKS_SCHEMA")
VOLUME_NAME = _required_env("PAYLOAD_VOLUME")
MODEL_URI = _required_env("MODEL_URI")
APP_NAME = os.environ.get("APP_NAME", "large-payload-app")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(64 * 1024 * 1024)))
VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME_NAME}"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

scorer_holder = ScorerHolder(MODEL_URI)
app = FastAPI(title="Large payload scoring API", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    scorer = scorer_holder.get()
    if scorer:
        logger.info("Modelo pronto: versão %s", scorer.model_version)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _parse_body(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Corpo maior que {MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"JSON inválido: {exc}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("payload"), dict):
        raise HTTPException(status_code=400, detail="O corpo deve ser um objeto com `payload` (objeto JSON)")
    return body


def _request_id(body: dict[str, Any]) -> str:
    """O Credit Engine pode enviar o próprio id (ex.: o do PEGA) para facilitar a comparação do shadow."""
    rid = body.get("request_id")
    if rid is None:
        return str(uuid.uuid4())
    if not _REQUEST_ID_RE.match(str(rid)):
        raise HTTPException(status_code=400, detail="request_id deve seguir [A-Za-z0-9_-]{1,64}")
    return str(rid)


@app.post("/invocations", status_code=202)
async def invocations(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    received_at = time.time()
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        # Responder antes de o upload terminar faz a maioria dos clientes ver "conexão
        # resetada" em vez do 413; por isso o corpo é lido e descartado (até 4x o limite).
        if int(content_length) <= 4 * MAX_BODY_BYTES:
            async for _ in request.stream():
                pass
        raise HTTPException(status_code=413, detail=f"Corpo maior que {MAX_BODY_BYTES} bytes")

    raw = await request.body()
    body = _parse_body(raw)
    try:
        scalars = extract_scalars(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request_id = _request_id(body)
    directory = storage.request_dir(VOLUME_ROOT, request_id)
    request_uri = f"{directory}/{storage.REQUEST_FILE}"
    response_uri = f"{directory}/{storage.RESPONSE_FILE}"

    # 202 durável: o corpo original já está no Volume quando confirmamos o recebimento.
    await run_in_threadpool(storage.write_bytes, request_uri, raw)
    accepted = {
        "request_id": request_id,
        "status": "queued",
        "payload_uri": request_uri,
        "response_uri": response_uri,
        "payload_hash": _sha256(raw),
        "payload_bytes": len(raw),
        "accept_latency_ms": (time.time() - received_at) * 1000.0,
    }
    job_store.create_job(request_id, accepted)
    logger.info("Aceito request_id=%s bytes=%s (o corpo não é logado)", request_id, len(raw))
    background_tasks.add_task(process_inference, accepted, scalars, body["payload"], received_at)
    return JSONResponse(status_code=202, content={**accepted, "status_url": f"/invocations/{request_id}"})


def process_inference(accepted: dict[str, Any], scalars: dict[str, float], payload: dict[str, Any], received_at: float) -> None:
    """Pontua, grava response_dbx.json e, por último, a linha de observabilidade."""
    request_id = accepted["request_id"]
    job_store.update_job(request_id, status="running")
    t_score = time.time()
    prediction = probability = error = None
    scorer = scorer_holder.get()
    try:
        if scorer is None:
            raise RuntimeError(scorer_holder.error or "Modelo ainda não carregado")
        prediction, probability = scorer.score(scalars, payload, request_id)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:4000]
        logger.exception("Falha no score request_id=%s", request_id)

    total_ms = (time.time() - received_at) * 1000.0
    result = {
        **accepted,
        "status": "failed" if error else "done",
        "prediction": prediction,
        "probability": probability,
        "model_uri": MODEL_URI,
        "model_version": scorer.model_version if scorer else None,
        "score_latency_ms": (time.time() - t_score) * 1000.0,
        "total_latency_ms": total_ms,
        "error_message": error,
        "source": APP_NAME,
    }
    try:
        storage.write_bytes(accepted["response_uri"], json.dumps(result).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao gravar response_dbx.json request_id=%s", request_id)
        result.update(status="failed", error_message=f"{error or ''}; gravação da resposta: {exc}"[:4000])
    job_store.update_job(request_id, **{k: v for k, v in result.items() if k != "request_id"})

    # Melhor esforço e por último: um cold start do SQL warehouse nunca atrasa a decisão.
    try:
        storage.insert_observability_row(
            {
                **result,
                "status": "ERROR" if error else "SUCCESS",
                "latency_ms": total_ms,
                "endpoint_name": APP_NAME,
                "model_alias": scorer.model_alias if scorer else None,
                "env": CATALOG,
                "scalars_json": json.dumps(scalars, separators=(",", ":")),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha na linha de observabilidade request_id=%s", request_id)
        job_store.update_job(request_id, observability_error=str(exc)[:1000])


@app.get("/invocations/{request_id}")
def get_invocation(request_id: str) -> dict[str, Any]:
    """Memória desta instância primeiro; depois o Volume (vale após restart e entre instâncias)."""
    job = job_store.get_job(request_id)
    if job is not None:
        return job
    if not _REQUEST_ID_RE.match(request_id):
        raise HTTPException(status_code=404, detail="request_id desconhecido")
    for directory in storage.candidate_dirs(VOLUME_ROOT, request_id):
        result = storage.read_json(f"{directory}/{storage.RESPONSE_FILE}")
        if result is not None:
            return result
        request_uri = f"{directory}/{storage.REQUEST_FILE}"
        if storage.exists(request_uri):
            return {"request_id": request_id, "status": "running", "payload_uri": request_uri}
    raise HTTPException(status_code=404, detail="request_id desconhecido")


@app.get("/health")
def health() -> dict[str, Any]:
    scorer = scorer_holder.get()
    return {
        "status": "ok" if scorer else "degraded",
        "model_ready": bool(scorer),
        "model_uri": MODEL_URI,
        "model_version": scorer.model_version if scorer else None,
        "model_load_error": scorer_holder.error,
        "volume_root": VOLUME_ROOT,
        "max_body_bytes": MAX_BODY_BYTES,
    }


if __name__ == "__main__":
    port = int(os.environ.get("DATABRICKS_APP_PORT", os.environ.get("PORT", "8000")))
    uvicorn.run(app, host="0.0.0.0", port=port)
