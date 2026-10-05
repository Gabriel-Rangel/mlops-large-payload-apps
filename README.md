# Score de payloads grandes com Databricks App

Solução para pontuar o modelo de decisão de crédito com requests de **até ~29 MB**
(15 variáveis + histórico completo do birô em JSON), em menos de um minuto.

## O problema

O fluxo shadow atual (API Gateway → Lambda → SQS → Lambda → Model Serving) não carrega
payloads grandes. Cada salto tem um limite menor que 29 MB:

| Salto | Limite |
|---|---|
| Lambda (invocação síncrona) | 6 MB |
| API Gateway | 10 MB |
| SQS / SNS | 1 MiB / 256 KB |
| Model Serving | 16 MB |

Medido num ambiente de teste com o mesmo desenho: **5 MB → 202; 9, 15 e 29 MB → 413**.

## A solução

Uma **Databricks App** passa a ser a porta de entrada. Ela não tem limite de tamanho de corpo
na plataforma (o código limita em 64 MiB), pontua o modelo no próprio processo e grava a
decisão no bucket do cliente, de onde segue pelo caminho de resposta que já existe.

```
Credit Engine ──① POST 29 MB (mesmo corpo de hoje, token OAuth)──► Databricks App
                ◄─② 202 + request_id ─────────────────────────────┘
App: ③ grava request.json no Volume → pontua o @Champion → grava response_dbx.json no Volume
Volume EXTERNAL = bucket S3 do cliente ──④ evento S3──► SNS ──► response router ──► Credit Engine
```

- **Volume EXTERNAL**: o Volume do Unity Catalog é um prefixo do bucket S3. Então
  `s3://<bucket>/payloads/.../request.json` e `/Volumes/<catálogo>/<schema>/payloads/.../request.json`
  são o mesmo arquivo, sem cópia entre AWS e Databricks.
- **Saem do caminho**: API Gateway, Lambda de entrada, SQS, Lambda de inferência e Model Serving.
- **Ficam**: o bucket, o SNS e o response router.
- **202 durável**: o `request.json` é gravado antes da resposta.

Detalhes, decisões e trade-offs: [`docs/arquitetura.md`](docs/arquitetura.md).

## Resultados (ambiente de teste, AWS us-east-1)

Do envio pela Lambda até a decisão chegar pelo SNS:

| Payload | POST → 202 | Total (decisão na fila) |
|---|---|---|
| 15 MB | 1,3 s | 2,6 s |
| 22 MB | 1,1 s | 2,8 s |
| **29 MB** | **1,3 s** | **3,1 s** |
| 32 MB | 1,4 s | 2,7 s |

O JSON de teste é sintético, mas realista (~180 mil registros de histórico em 29 MB). Acima de
64 MiB a App responde 413.

## Estrutura

```
databricks.yml          bundle: variáveis e ambientes (dev / prod)
resources/              schema + Volume, jobs, App
app/                    código da App (FastAPI) — ver app/README.md
src/model/              modelo PyFunc + features (vai junto com o modelo para o Unity Catalog)
src/common/payloads.py  gerador de payload sintético para os testes
src/notebooks/          01 setup UC · 02 treino · 03 gates/promoção · 04 validação da App
scripts/                service principal e external location do bucket — ver scripts/README.md
aws/                    exemplo AWS para teste ponta a ponta — ver aws/README.md
tests/                  testes unitários (sem workspace)
docs/arquitetura.md     decisões, limites e trade-offs
```

## Como implantar

Pré-requisitos: Databricks CLI autenticada (`databricks auth login`), um catálogo do Unity Catalog,
um SQL warehouse e permissão para criar storage credential / external location.

**1. Ajuste o `databricks.yml`** no target desejado: `catalog`, `warehouse_id` e `payload_s3_url`.
Sem bucket ainda, use `payload_volume_type: MANAGED` e remova `payload_s3_url`.

**2. Bucket no Unity Catalog** (para o Volume EXTERNAL):

```bash
python scripts/setup_uc_external_volume.py --profile <PERFIL> --aws-profile <PERFIL_AWS> \
  --bucket <bucket> --name <nome_uc> --role-name <role-para-o-unity-catalog>
```

**3. Service principal do Credit Engine:**

```bash
python scripts/setup_identities.py --profile <PERFIL>
```

**4. Bundle e ciclo de vida do modelo:**

```bash
databricks bundle deploy -t dev -p <PERFIL>
databricks bundle run setup_uc -t dev -p <PERFIL>
databricks bundle run train_register -t dev -p <PERFIL>
databricks bundle run large_payload_app -t dev -p <PERFIL>        # sobe a App (ainda sem modelo)
databricks bundle run evaluate_promote -t dev -p <PERFIL> --params promote_to_champion=true
```

A App tenta carregar o `@Champion` a cada 30 s, então pega o modelo assim que ele é promovido.

**5. Validação:**

```bash
databricks bundle run validate_app -t dev -p <PERFIL>     # 0,01 a 32 MB, SLO, Volume, observabilidade, 413
aws/deploy.sh && aws/run_e2e.sh 15 22 29 32                # ponta a ponta a partir da AWS (opcional)
```

**Testes unitários** (sem workspace): `python -m unittest discover -s tests -t .`
(precisa de `pandas`, `mlflow`, `scikit-learn`, `fastapi`, `httpx`, `boto3`).

## O que muda para o Credit Engine

1. A URL do POST passa a ser a da App (`<app-url>/invocations`), com o mesmo corpo.
2. Autenticação por **token OAuth** do service principal `credit-engine-client` (client credentials,
   validade de 1 hora). Apps não aceitam PAT.
3. O response router lê o `response_dbx.json` indicado no evento S3.

## Operação

- **Novo modelo**: promova com `evaluate_promote` e reinicie a App.
- **Disponibilidade**: em produção, rode a App com 2 instâncias. O status (`GET /invocations/{id}`)
  é lido do Volume quando a instância não conhece o request.
- **Observabilidade**: tabela `inference_observability` (uma linha por request, com latências e o
  ponteiro para o `request.json`); o JSON completo fica só no Volume.
- **Retenção**: o exemplo AWS expira os payloads em 30 dias; defina a política real com risco e compliance.
