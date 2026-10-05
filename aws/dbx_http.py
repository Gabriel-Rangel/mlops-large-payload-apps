"""HTTP (biblioteca padrão) + OAuth M2M da Databricks para as Lambdas, sem layer de dependências.

As credenciais do service principal ficam no AWS Secrets Manager como
{"client_id", "client_secret", "host"} (gravadas por scripts/setup_identities.py).
O token é reaproveitado entre invocações e renovado 2 min antes de expirar.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import boto3

_creds: dict[str, str] | None = None
_token: tuple[str | None, float] = (None, 0.0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Expõe redirects (ex.: tela de login da App) em vez de segui-los silenciosamente."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120,
) -> tuple[int, dict[str, str], bytes]:
    """Faz a chamada e devolve (status, headers, corpo) também para respostas 4xx/5xx."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def databricks_credentials() -> dict[str, str]:
    global _creds
    if _creds is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["DATABRICKS_SECRET_ID"])
        _creds = json.loads(secret["SecretString"])
    return _creds


def databricks_host() -> str:
    return databricks_credentials()["host"].rstrip("/")


def databricks_token() -> str:
    """Token OAuth M2M (client_credentials) do service principal, com cache."""
    global _token
    token, expires_at = _token
    if token and time.time() < expires_at - 120:
        return token
    creds = databricks_credentials()
    basic = base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    status, _, body = http(
        "POST",
        f"{databricks_host()}/oidc/v1/token",
        urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode(),
        {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if status != 200:
        raise RuntimeError(f"Falha ao obter token OAuth: HTTP {status} {body[:300]!r}")
    data = json.loads(body)
    _token = (data["access_token"], time.time() + float(data.get("expires_in", 3600)))
    return data["access_token"]
