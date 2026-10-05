"""Lambda que simula o Credit Engine chamando a Databricks App.

Gera um request sintético (15 variáveis + JSON do birô) do tamanho pedido, faz o POST na App
e mede o tempo até a decisão chegar pelo caminho de produção:
response_dbx.json no S3 → SNS → fila inbox (no lugar do response router).

Evento: {"payload_mb": 29, "app_url": "https://<app>.databricksapps.com", "timeout_s": 150}
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any

import boto3

import dbx_http
from payloads import make_request_body

s3 = boto3.client("s3")
sqs = boto3.client("sqs")
BUCKET = os.environ["BUCKET"]
INBOX_URL = os.environ["INBOX_URL"]


def _ms(t0: float) -> float:
    return round((time.time() - t0) * 1000.0, 1)


def s3_keys(message_body: str) -> list[str]:
    """Chaves S3 de uma mensagem da inbox (evento S3 cru, ou envelope SNS se a entrega não for raw)."""
    event = json.loads(message_body)
    if "Message" in event:
        event = json.loads(event["Message"])
    return [urllib.parse.unquote_plus(r["s3"]["object"]["key"]) for r in event.get("Records", [])]


def wait_for_decision(request_id: str, deadline: float) -> tuple[dict[str, Any] | None, str | None]:
    """Long polling na inbox. A fila é só de teste: mensagens de outros requests também são apagadas."""
    while time.time() < deadline:
        wait = max(1, min(20, int(deadline - time.time())))
        for msg in sqs.receive_message(QueueUrl=INBOX_URL, MaxNumberOfMessages=10, WaitTimeSeconds=wait).get("Messages", []):
            sqs.delete_message(QueueUrl=INBOX_URL, ReceiptHandle=msg["ReceiptHandle"])
            for key in s3_keys(msg["Body"]):
                if f"/{request_id}/" in key:
                    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()), key
    return None, None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    size_mb = float(event.get("payload_mb", 29))
    timeout_s = float(event.get("timeout_s", 150))
    t_gen = time.time()
    body = make_request_body(size_mb, seed=int(size_mb * 100) + 3)
    result: dict[str, Any] = {"payload_mb": size_mb, "body_bytes": len(body), "generate_ms": _ms(t_gen)}

    token = dbx_http.databricks_token()
    t0 = time.time()
    status, _, resp = dbx_http.http(
        "POST", f"{event['app_url'].rstrip('/')}/invocations", body,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=timeout_s,
    )
    result["time_to_202_ms"] = _ms(t0)
    if status != 202:
        result.update(ok=False, http=status, body=resp[:300].decode(errors="replace"))
        return result

    request_id = json.loads(resp)["request_id"]
    decision, key = wait_for_decision(request_id, t0 + timeout_s)
    result.update(
        ok=bool(decision and decision.get("status") == "done"),
        request_id=request_id,
        e2e_ms=_ms(t0),  # do início do POST até a decisão chegar na inbox
        prediction=(decision or {}).get("prediction"),
        app_total_latency_ms=(decision or {}).get("total_latency_ms"),
        response_key=key,
    )
    print(json.dumps(result))
    return result
