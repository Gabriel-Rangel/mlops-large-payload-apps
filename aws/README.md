# Exemplo AWS

Reproduz, numa conta AWS de teste, o lado AWS da solução e mede o tempo ponta a ponta como o
Credit Engine veria.

```
Lambda "Credit Engine" ──POST 29 MB──► Databricks App ──► response_dbx.json no bucket
bucket (evento S3) ──► SNS ──► fila inbox (no lugar do response router) ──► Lambda mede o tempo
```

| Arquivo | Papel |
|---|---|
| `template.yaml` | CloudFormation: bucket de payloads, notificação S3 → SNS, fila inbox, Lambda simuladora |
| `credit_engine_sim.py` | Lambda que gera o request, faz o POST na App e espera a decisão na inbox |
| `dbx_http.py` | HTTP + token OAuth da Databricks com biblioteca padrão (sem layer) |
| `deploy.sh` / `run_e2e.sh` / `teardown.sh` | implantar, testar e remover |

## Em produção

Nada disso precisa ser criado do zero. Use o **bucket que já recebe o `request.json`** e o
**response router** atual, e adicione só:

1. a external location do Unity Catalog no bucket (`scripts/setup_uc_external_volume.py`);
2. a notificação do bucket para o SNS atual, com filtro de sufixo `response_dbx.json`
   (o trecho `NotificationConfiguration` do `template.yaml`).

O response router passa a receber um **evento S3** (com a chave do `response_dbx.json`) em vez da
mensagem que a Lambda de inferência publica hoje. Basta ler o arquivo indicado no evento.

## Uso

```bash
export AWS_PROFILE=<perfil>
aws/deploy.sh                       # stack "lp-apps" (mude com PREFIX=...)
aws/run_e2e.sh 15 22 29 32          # depois que a App estiver no ar
aws/teardown.sh                     # remove tudo (pede confirmação)
```

O `run_e2e.sh` usa `DATABRICKS_PROFILE` e `APP_NAME` para descobrir a URL da App. O resultado de
cada execução vai para `build/e2e_results.jsonl`.
