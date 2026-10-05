# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Validação da App (requests de até 32 MB)
# MAGIC
# MAGIC Simula o Credit Engine: um único POST com as 15 variáveis **e** o JSON do birô, autenticado
# MAGIC com o OAuth do service principal cliente. Para cada tamanho: 202 → consulta até `done`.
# MAGIC
# MAGIC | Check | O que prova |
# MAGIC |---|---|
# MAGIC | `health_model_ready` | a App carregou o `@Champion` |
# MAGIC | `http_202` | a App aceitou o corpo inteiro |
# MAGIC | `done_with_prediction` | score feito no processo da App |
# MAGIC | `e2e_under_slo` | do POST à decisão abaixo do SLA (padrão 60 s) |
# MAGIC | `request_json_in_volume` | o `request.json` no Volume é idêntico ao enviado (202 durável) |
# MAGIC | `response_dbx_in_volume` | o `response_dbx.json` foi gravado (no S3, dispara o SNS) |
# MAGIC | `observability_row` | linha de metadados gravada, sem o JSON completo |
# MAGIC | `oversize_413` | corpo acima de `MAX_BODY_BYTES` é recusado com 413 |
# MAGIC
# MAGIC Qualquer check com FAIL faz o job falhar. O resultado volta em `notebook_output`.

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("app_name", "")
dbutils.widgets.text("secret_scope", "mlops-large-payload-apps")
dbutils.widgets.text("payload_sizes_mb", "0.01,15,22,29,32")
dbutils.widgets.text("oversize_mb", "65")
dbutils.widgets.text("slo_ms", "60000")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
app_name = dbutils.widgets.get("app_name")
secret_scope = dbutils.widgets.get("secret_scope")
payload_sizes_mb = [float(x) for x in dbutils.widgets.get("payload_sizes_mb").split(",") if x.strip()]
oversize_mb = float(dbutils.widgets.get("oversize_mb") or 0)
slo_ms = float(dbutils.widgets.get("slo_ms"))
obs_table = f"{catalog}.{schema}.inference_observability"
timeout_s = 180

# COMMAND ----------

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

root = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src" / "common" / "payloads.py").exists())
sys.path.insert(0, str(root))
from src.common.payloads import make_request_body  # noqa: E402

ws = WorkspaceClient()
host = ws.config.host.rstrip("/")
app_url = ws.apps.get(app_name).url.rstrip("/")
results: list[dict[str, Any]] = []
latency_rows: list[dict[str, Any]] = []


def record(size_mb: float | None, check: str, passed: bool, detail: str, latency_ms: float | None = None) -> None:
    results.append({"size_mb": size_mb, "check": check, "passed": "PASS" if passed else "FAIL",
                    "detail": detail[:1500], "latency_ms": None if latency_ms is None else round(latency_ms, 1)})


def oauth_token() -> str:
    """Token OAuth M2M do service principal cliente (Apps não aceitam PAT)."""
    resp = requests.post(
        f"{host}/oidc/v1/token",
        auth=(dbutils.secrets.get(secret_scope, "client-id"), dbutils.secrets.get(secret_scope, "client-secret")),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


headers = {"Authorization": f"Bearer {oauth_token()}"}

# COMMAND ----------

# A App tenta carregar o Champion a cada 30 s até conseguir.
health: dict[str, Any] = {}
deadline = time.time() + 300
while time.time() < deadline:
    resp = requests.get(f"{app_url}/health", headers=headers, timeout=30, allow_redirects=False)
    health = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"http": resp.status_code}
    if health.get("model_ready"):
        break
    time.sleep(10)
record(None, "health_model_ready", bool(health.get("model_ready")), json.dumps(health))

# COMMAND ----------


def observability_row(request_id: str) -> pd.DataFrame | None:
    """A linha é gravada depois da decisão (melhor esforço); espera até ~1 min."""
    for _ in range(12):
        df = spark.table(obs_table).filter(F.col("request_id") == request_id).toPandas()
        if len(df):
            return df
        time.sleep(5)
    return None


for size_mb in payload_sizes_mb:
    body = make_request_body(size_mb, seed=int(size_mb * 100) + 1)
    t0 = time.time()
    post = requests.post(f"{app_url}/invocations", headers={**headers, "Content-Type": "application/json"},
                         data=body, timeout=(30, timeout_s), allow_redirects=False)
    t_202 = (time.time() - t0) * 1000.0
    accepted = post.json() if post.headers.get("content-type", "").startswith("application/json") else {}
    request_id = accepted.get("request_id")
    record(size_mb, "http_202", post.status_code == 202 and bool(request_id), f"HTTP {post.status_code} {str(accepted)[:300]}", t_202)
    if not request_id:
        continue

    job: dict[str, Any] = {}
    while time.time() - t0 < timeout_s:
        job = requests.get(f"{app_url}/invocations/{request_id}", headers=headers, timeout=30).json()
        if job.get("status") in ("done", "failed"):
            break
        time.sleep(0.5)
    e2e_ms = (time.time() - t0) * 1000.0
    record(size_mb, "done_with_prediction", job.get("status") == "done" and job.get("prediction") is not None,
           f"status={job.get('status')} prediction={job.get('prediction')} erro={job.get('error_message')}")
    record(size_mb, "e2e_under_slo", e2e_ms < slo_ms, f"POST → done = {e2e_ms:.0f} ms (SLO {slo_ms:.0f} ms)", e2e_ms)

    # Jobs montam /Volumes via FUSE: os arquivos gravados pela App são lidos direto.
    stored = Path(accepted["payload_uri"])
    same = stored.exists() and hashlib.sha256(stored.read_bytes()).digest() == hashlib.sha256(body).digest()
    record(size_mb, "request_json_in_volume", same, f"{stored} idêntico ao enviado: {same}")
    response_path = Path(accepted["response_uri"])
    response = json.loads(response_path.read_text()) if response_path.exists() else {}
    record(size_mb, "response_dbx_in_volume", response.get("status") == "done", f"{response_path} status={response.get('status')}")

    obs = observability_row(request_id)
    longest = 0 if obs is None else int(obs.astype(str).map(len).max().max())
    record(size_mb, "observability_row", obs is not None and longest < 20_000,
           "linha não encontrada" if obs is None else f"maior campo texto = {longest} caracteres")
    latency_rows.append({
        "size_mb": size_mb,
        "time_to_202_ms": round(t_202),
        "app_accept_ms": round(float(response.get("accept_latency_ms") or 0)),
        "app_score_ms": round(float(response.get("score_latency_ms") or 0)),
        "app_total_ms": round(float(response.get("total_latency_ms") or 0)),
        "client_e2e_ms": round(e2e_ms),
    })

# COMMAND ----------

if oversize_mb > 0:
    big = make_request_body(oversize_mb, seed=999)
    try:
        resp = requests.post(f"{app_url}/invocations", headers={**headers, "Content-Type": "application/json"},
                             data=big, timeout=(30, timeout_s), allow_redirects=False)
        record(oversize_mb, "oversize_413", resp.status_code == 413, f"HTTP {resp.status_code}")
    except requests.RequestException as exc:
        record(oversize_mb, "oversize_413", False, f"{type(exc).__name__}: {exc} (sem status HTTP)")

# COMMAND ----------

latency = pd.DataFrame(latency_rows)
summary = pd.DataFrame(results)
display(latency)
display(summary)
failed = summary[summary["passed"] == "FAIL"]
if len(failed):
    raise RuntimeError("Validação falhou: " + "; ".join(f"{r.size_mb}/{r.check}: {r.detail[:300]}" for r in failed.itertuples()))
dbutils.notebook.exit(json.dumps({"latency": latency_rows, "checks": summary.to_dict("records")}))
