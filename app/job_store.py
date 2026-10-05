"""Cache em memória do status dos requests (por processo).

A fonte da verdade são os arquivos no Volume (request.json / response_dbx.json): o GET
consulta o Volume quando o id não está aqui, então o status sobrevive a restart e funciona
com várias instâncias da App.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(request_id: str, fields: dict[str, Any]) -> None:
    with _lock:
        _jobs[request_id] = {"request_id": request_id, "created_at": _now(), "updated_at": _now(), **fields}


def update_job(request_id: str, /, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(request_id)
        if job is not None:
            job.update(fields, updated_at=_now())


def get_job(request_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(request_id)
        return dict(job) if job else None
