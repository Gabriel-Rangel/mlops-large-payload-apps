"""Engenharia de features do modelo de demonstração.

Cada request tem 15 variáveis escalares e um JSON grande (histórico do birô, 15–29 MB).
O JSON nunca vai no corpo do Model Serving (limite de 16 MB): ele fica num Volume do
Unity Catalog e o modelo lê o arquivo pelo caminho (`file_path`), ou recebe o objeto já
em memória (Databricks App).

Substitua `SCALAR_FEATURES` e `derive_features_from_json` pelas features do modelo real.
Esta pasta é logada junto com o modelo, então App e Model Serving usam exatamente este código.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Variáveis escalares do request (nomes de demonstração).
SCALAR_FEATURES = [f"var_{i:02d}" for i in range(1, 16)]

# Colunas opcionais do request. `file_path` (contrato do cliente) e `payload_uri` são sinônimos.
REFERENCE_COLUMNS = ["file_path", "payload_uri"]
OPTIONAL_REQUEST_COLUMNS = REFERENCE_COLUMNS + ["payload_hash", "request_id", "payload"]

# Features derivadas do JSON do birô.
JSON_DERIVED_FEATURES = [
    "json_n_items",
    "json_total_amount",
    "json_avg_amount",
    "json_max_amount",
    "json_unique_categories",
]


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


_ws_client = None


def _workspace_client():
    """WorkspaceClient criado sob demanda (só é usado quando /Volumes não está montado)."""
    global _ws_client
    if _ws_client is None:
        from databricks.sdk import WorkspaceClient

        _ws_client = WorkspaceClient()
    return _ws_client


def read_payload_bytes(path: str) -> bytes:
    """Lê o arquivo do Volume: POSIX quando /Volumes está montado (jobs), Files API caso contrário.

    O container do Model Serving NÃO monta /Volumes, então `open("/Volumes/...")` falha mesmo
    com READ VOLUME. A Files API funciona com esse grant, usando o service principal configurado
    nas environment_vars do endpoint (DATABRICKS_HOST / DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET).
    """
    local = Path(path)
    if local.exists():
        return local.read_bytes()
    if not path.startswith("/Volumes/"):
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")
    try:
        with _workspace_client().files.download(path).contents as stream:
            return stream.read()
    except Exception as exc:  # noqa: BLE001 — erro claro para quem chama o endpoint
        raise FileNotFoundError(
            f"Não foi possível ler {path} pela Files API. Dê READ VOLUME (e USE CATALOG / USE SCHEMA) "
            f"ao service principal do endpoint. Erro: {exc}"
        ) from exc


def derive_features_from_json(payload: dict[str, Any]) -> dict[str, float]:
    """Reduz o JSON do birô a poucas features numéricas (substitua pela lógica real)."""
    items = payload.get("items") or []
    amounts = [float(i.get("amount", 0.0)) for i in items if isinstance(i, dict)]
    categories = {str(i.get("category")) for i in items if isinstance(i, dict) and i.get("category") is not None}
    n = len(amounts)
    total = float(sum(amounts)) if amounts else 0.0
    return {
        "json_n_items": float(n),
        "json_total_amount": total,
        "json_avg_amount": total / n if n else 0.0,
        "json_max_amount": float(max(amounts)) if amounts else 0.0,
        "json_unique_categories": float(len(categories)),
    }


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and np.isnan(value):
        return False
    return str(value) not in ("", "nan", "None")


def _field(rec: Any, name: str) -> Any:
    if name not in getattr(rec, "index", []):
        return None
    value = rec.get(name)
    return value if _present(value) else None


def _inline_payload(rec: Any) -> dict[str, Any] | None:
    """JSON enviado no próprio request (`payload`): dict (App) ou string (Model Serving)."""
    raw = _field(rec, "payload")
    if raw is None:
        return None
    return raw if isinstance(raw, dict) else json.loads(str(raw))


def resolve_request(rec: Any) -> tuple[dict[str, float], dict[str, Any]]:
    """Devolve (variáveis escalares, JSON do birô) de uma linha do request.

    O JSON vem do `payload` inline ou do arquivo em `file_path` / `payload_uri`, lido UMA vez
    (o `payload_hash` opcional é conferido nesses mesmos bytes). O arquivo pode ser só o JSON do
    birô ou o request original completo `{var_01..var_15, payload}`; variáveis da linha têm
    prioridade sobre as do arquivo. Assim, `{"file_path": "/Volumes/..."}` sozinho é um request válido.
    """
    doc = _inline_payload(rec)
    if doc is None:
        uri = next((_field(rec, c) for c in REFERENCE_COLUMNS if _field(rec, c) is not None), None)
        if uri is None:
            raise ValueError("Informe `file_path`, `payload_uri` ou `payload` inline")
        raw = read_payload_bytes(str(uri))
        expected = _field(rec, "payload_hash")
        if expected is not None and sha256_bytes(raw) != str(expected):
            raise ValueError(f"payload_hash divergente para {uri}: esperado {expected}, obtido {sha256_bytes(raw)}")
        doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("O JSON do payload deve ser um objeto")

    envelope: dict[str, Any] = doc if isinstance(doc.get("payload"), dict) else {}
    bureau = envelope["payload"] if envelope else doc
    scalars: dict[str, float] = {}
    missing = []
    for col in SCALAR_FEATURES:
        value = _field(rec, col)
        if value is None:
            value = envelope.get(col)
        if value is None:
            missing.append(col)
        else:
            scalars[col] = float(value)
    if missing:
        raise ValueError(f"Variáveis ausentes (no request e no arquivo): {missing}")
    return scalars, bureau


def build_feature_frame(records: pd.DataFrame) -> pd.DataFrame:
    """Matriz numérica do modelo sklearn: variáveis escalares + features do JSON."""
    rows = []
    for _, rec in records.iterrows():
        scalars, bureau = resolve_request(rec)
        rows.append({**scalars, **derive_features_from_json(bureau)})
    return pd.DataFrame(rows, columns=SCALAR_FEATURES + JSON_DERIVED_FEATURES)


def make_synthetic_training_frame(n: int = 400, seed: int = 7) -> tuple[pd.DataFrame, pd.Series]:
    """Dados rotulados sintéticos para o job de treino de demonstração (sem dados do cliente)."""
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(
        rng.normal(size=(n, len(SCALAR_FEATURES) + len(JSON_DERIVED_FEATURES))),
        columns=SCALAR_FEATURES + JSON_DERIVED_FEATURES,
    )
    score = (
        0.4 * X["var_01"]
        + 0.3 * X["var_02"]
        + 0.2 * X["json_total_amount"]
        + 0.15 * X["json_n_items"]
        + rng.normal(scale=0.5, size=n)
    )
    return X, pd.Series((score > 0).astype(int), name="label")
