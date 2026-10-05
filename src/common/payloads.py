"""Gerador de payloads sintéticos de birô de crédito (testes, notebooks e Lambdas).

O JSON real é o histórico completo do cliente no birô (15–29 MB). Um único campo de
enchimento faria o parse e as features parecerem instantâneos; por isso o gerador cria
muitos registros pequenos de histórico, como o documento real.

Só biblioteca padrão: o mesmo arquivo é copiado para o pacote das Lambdas.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import date, timedelta
from typing import Any

SCALAR_FEATURES = [f"var_{i:02d}" for i in range(1, 16)]

_CATEGORIES = ["CARD", "PERSONAL_LOAN", "MORTGAGE", "AUTO", "OVERDRAFT", "PAYROLL", "UTILITY"]
_STATUSES = ["PAID", "OPEN", "LATE", "RENEGOTIATED", "WRITTEN_OFF"]


def serialize(obj: Any) -> bytes:
    """JSON compacto em UTF-8 (mesma codificação dos arquivos request.json)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def default_scalars() -> dict[str, float]:
    return {c: round(i * 0.01, 2) for i, c in enumerate(SCALAR_FEATURES, start=1)}


def _history_record(rng: random.Random, n: int) -> dict[str, Any]:
    opened = date(2010, 1, 1) + timedelta(days=rng.randrange(5800))
    status = rng.choice(_STATUSES)
    return {
        "contract_id": f"C{n:09d}",
        "date": opened.isoformat(),
        "creditor": f"CREDITOR_{rng.randrange(400):03d}",
        "category": rng.choice(_CATEGORIES),
        "amount": round(rng.uniform(50, 25000), 2),
        "installments": rng.choice([1, 3, 6, 10, 12, 18, 24, 36, 48, 60]),
        "status": status,
        "days_past_due": rng.randrange(1, 180) if status in ("LATE", "WRITTEN_OFF") else 0,
    }


def make_bureau_payload(target_mb: float, seed: int = 7, tag: str = "synthetic") -> dict[str, Any]:
    """JSON de birô com ~target_mb MiB serializado (diferença menor que um registro)."""
    target = int(target_mb * 1024 * 1024)
    rng = random.Random(seed)
    doc: dict[str, Any] = {
        "customer": {"document_hash": f"{rng.getrandbits(128):032x}", "score_bureau": rng.randrange(300, 1000)},
        "meta": {"generated_for": tag, "schema": "bureau-history-v1", "target_mb": target_mb},
        "items": [],
    }
    size = len(serialize(doc))
    items = doc["items"]
    while True:
        record = _history_record(rng, len(items))
        # +1 pela vírgula entre os elementos do array.
        record_size = len(serialize(record)) + (1 if items else 0)
        if size + record_size > target and items:
            break
        items.append(record)
        size += record_size
    return doc


def make_request_body(target_mb: float, seed: int = 7, request_id: str | None = None) -> bytes:
    """Request completo como o Credit Engine envia: 15 variáveis + `payload` (JSON do birô).

    Montado por concatenação para serializar o payload de vários MB uma única vez.
    """
    head: dict[str, Any] = dict(default_scalars())
    if request_id:
        head["request_id"] = request_id
    head_json = serialize(head)
    payload_json = serialize(make_bureau_payload(target_mb, seed=seed, tag="request"))
    return head_json[:-1] + b',"payload":' + payload_json + b"}"
