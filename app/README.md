# App — API de score

FastAPI executada como Databricks App. Recebe o request completo do Credit Engine, persiste no
Volume, responde **202** e pontua o modelo `@Champion` no próprio processo.

| Arquivo | Papel |
|---|---|
| `app.py` | rotas `POST /invocations`, `GET /invocations/{id}`, `GET /health` |
| `scoring.py` | carrega o Champion do Unity Catalog e chama o `predict` do próprio modelo |
| `storage.py` | `request.json` / `response_dbx.json` no Volume (Files API) e linha de observabilidade |
| `job_store.py` | cache de status em memória (a fonte da verdade é o Volume) |
| `requirements.txt` | dependências instaladas no deploy da App |

A configuração (catálogo, schema, Volume, modelo, warehouse) vem de variáveis de ambiente
definidas em [`../resources/app.yml`](../resources/app.yml).

## Autenticação

Databricks Apps aceitam **token OAuth**, não PAT. O Credit Engine usa um service principal
(client credentials):

```bash
TOKEN=$(curl -s -u "$CLIENT_ID:$CLIENT_SECRET" "$DATABRICKS_HOST/oidc/v1/token" \
  -d grant_type=client_credentials -d scope=all-apis | jq -r .access_token)
```

O token vale 1 hora: gere, reaproveite e renove antes de expirar. O service principal precisa de
`CAN_USE` na App e do entitlement `workspace-access`. Sem esse entitlement, a App redireciona o
token para a tela de login (HTTP 302).

## `POST /invocations`

O mesmo corpo enviado hoje ao PEGA. `request_id` é opcional; use o id de correlação do Credit
Engine para cruzar com a resposta do PEGA no shadow mode.

```json
{
  "request_id": "pega-123",
  "var_01": 0.01, "var_02": 0.02, "...": "...", "var_15": 0.15,
  "payload": { "customer": { "...": "..." }, "items": [ { "amount": 1520.3, "category": "CARD", "...": "..." } ] }
}
```

```bash
curl -sS -X POST "$APP_URL/invocations" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" --data-binary @request.json
```

Resposta **202**, depois que o `request.json` já está gravado no Volume:

```json
{
  "request_id": "pega-123",
  "status": "queued",
  "payload_uri": "/Volumes/<catálogo>/<schema>/payloads/dt=2026-10-05/pega-123/request.json",
  "response_uri": "/Volumes/<catálogo>/<schema>/payloads/dt=2026-10-05/pega-123/response_dbx.json",
  "status_url": "/invocations/pega-123"
}
```

| Status | Quando |
|---|---|
| 202 | aceito e persistido |
| 400 | JSON inválido, `payload` ausente ou variável faltando |
| 413 | corpo acima de `MAX_BODY_BYTES` (64 MiB por padrão) |

## `GET /invocations/{request_id}`

```json
{"request_id": "pega-123", "status": "done", "prediction": 1, "probability": 0.93,
 "model_version": "2", "accept_latency_ms": 1251, "score_latency_ms": 332, "total_latency_ms": 1584}
```

`status`: `queued` → `running` → `done` | `failed`. Funciona após restart da App e com várias
instâncias, porque consulta o `response_dbx.json` no Volume quando a instância não conhece o id.

No fluxo com o bucket do cliente, o Credit Engine **não precisa** fazer polling na App: o
`response_dbx.json` gravado no S3 dispara o SNS e a decisão chega pelo response router.

## Pontos de operação

- **Novo Champion:** a App carrega o modelo no startup. Depois de promover uma versão, reinicie a
  App (`databricks bundle run large_payload_app`).
- **Request interrompido:** se a App reiniciar entre o 202 e a decisão, o request fica `running`
  (o `request.json` está salvo). Para produção, vale um job que reprocesse requests sem resposta.
- **Disponibilidade:** em produção, use 2 instâncias da App. O status já funciona com várias instâncias.
