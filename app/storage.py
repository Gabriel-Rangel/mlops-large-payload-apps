"""Arquivos no Volume (Files API) e linha de observabilidade (SQL warehouse).

Layout por request (igual ao bucket S3 quando o Volume é EXTERNAL):
  /Volumes/<catálogo>/<schema>/<volume>/dt=AAAA-MM-DD/<request_id>/request.json        corpo original
  /Volumes/<catálogo>/<schema>/<volume>/dt=AAAA-MM-DD/<request_id>/response_dbx.json   decisão

As Databricks Apps não montam /Volumes: todo acesso é pela Files API.
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound

logger = logging.getLogger("large-payload-app.storage")

REQUEST_FILE = "request.json"
RESPONSE_FILE = "response_dbx.json"
_client: WorkspaceClient | None = None


def workspace_client() -> WorkspaceClient:
    """Cliente único; usa as credenciais do service principal da App, injetadas pela plataforma."""
    global _client
    if _client is None:
        _client = WorkspaceClient()
    return _client


def request_dir(volume_root: str, request_id: str, dt: datetime | None = None) -> str:
    day = (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"{volume_root}/dt={day}/{request_id}"


def candidate_dirs(volume_root: str, request_id: str) -> list[str]:
    """Hoje e ontem (UTC): onde a consulta de um request_id pode encontrar os arquivos."""
    now = datetime.now(timezone.utc)
    return [request_dir(volume_root, request_id, now - timedelta(days=d)) for d in (0, 1)]


def write_bytes(uri: str, raw: bytes) -> None:
    workspace_client().files.upload(uri, io.BytesIO(raw), overwrite=True)
    logger.info("Gravado %s (%s bytes)", uri.rsplit("/", 1)[-1], len(raw))


def read_json(uri: str) -> dict[str, Any] | None:
    try:
        resp = workspace_client().files.download(uri)
    except NotFound:
        return None
    with resp.contents as stream:
        return json.loads(stream.read().decode("utf-8"))


def exists(uri: str) -> bool:
    try:
        workspace_client().files.get_metadata(uri)
        return True
    except NotFound:
        return False


_OBS_COLUMNS = [
    "request_id", "endpoint_name", "model_version", "model_alias", "status", "latency_ms",
    "accept_latency_ms", "total_latency_ms", "payload_uri", "payload_hash", "payload_bytes",
    "prediction", "probability", "error_message", "env", "scalars_json",
]


def _sql_literal(value: Any) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "NULL"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def insert_observability_row(row: dict[str, Any]) -> None:
    """Uma linha de metadados por request (o JSON completo fica só no Volume)."""
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    table = os.environ.get("OBSERVABILITY_TABLE")
    if not warehouse_id or not table:
        raise RuntimeError("DATABRICKS_WAREHOUSE_ID / OBSERVABILITY_TABLE não definidos")
    values = ", ".join(_sql_literal(row.get(c)) for c in _OBS_COLUMNS)
    statement = (
        f"INSERT INTO {table} ({', '.join(_OBS_COLUMNS)}, event_ts, ingested_at) "
        f"VALUES ({values}, current_timestamp(), current_timestamp())"
    )
    resp = workspace_client().statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=statement, wait_timeout="50s"
    )
    state = getattr(getattr(resp.status, "state", None), "value", None)
    if state in {"FAILED", "CANCELED", "CLOSED"}:
        raise RuntimeError(f"INSERT de observabilidade falhou: {resp.status.error or state}")
