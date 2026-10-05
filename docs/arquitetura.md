# Arquitetura e decisões

## Requisitos

- Modelo de decisão de crédito rodando em shadow mode contra o PEGA até o cutover.
- Request: 15–16 variáveis + histórico completo do birô em JSON, de 15 a 29 MB. O histórico ainda
  não pode ser descartado.
- ~80 requests por dia. A meta é decidir em menos de 1 minuto (hoje a janela é de até 3 min).
- Rastreabilidade: guardar o request e a decisão; comparar com a resposta do PEGA.

## Por que uma Databricks App

| Componente | Limite de corpo |
|---|---|
| Lambda (invocação síncrona) | 6 MB |
| API Gateway | 10 MB |
| SQS / SNS | 1 MiB / 256 KB |
| Model Serving | 16 MB |
| **Databricks App** | **sem limite na plataforma** (a App limita em 64 MiB, configurável) |

A App recebe o mesmo corpo que o Credit Engine envia hoje, sem fatiar nem mudar o formato.
Ela também pontua o modelo no próprio processo, então não há um segundo salto HTTP com o
payload grande.

## Fluxo

1. O Credit Engine faz `POST /invocations` com o corpo completo e um token OAuth.
2. A App grava o corpo original como `request.json` no Volume e só então responde **202** com o
   `request_id`. O aceite é durável.
3. Em segundo plano, a App pontua o `@Champion` com o `predict` do próprio modelo, ou seja, com o
   mesmo código de features registrado no Unity Catalog.
4. A App grava `response_dbx.json` ao lado do `request.json`.
5. Como o Volume é EXTERNAL (um prefixo do bucket), o arquivo aparece no S3 e a notificação do
   bucket publica no SNS. A partir daí, o caminho de resposta é o de hoje.
6. Por último, e sem atrasar a decisão, a App grava uma linha em `inference_observability`.

Layout no bucket / Volume:

```
payloads/dt=AAAA-MM-DD/<request_id>/request.json        corpo original (15 variáveis + payload)
payloads/dt=AAAA-MM-DD/<request_id>/response_dbx.json   decisão, versão do modelo e latências
```

`response.json` (PEGA) pode ficar no mesmo lugar: a comparação do shadow mode vira uma query
por `request_id`.

## Decisões

| Decisão | Motivo |
|---|---|
| Volume **EXTERNAL** no bucket do cliente | A AWS continua gravando e lendo com IAM; a Databricks acessa o mesmo objeto com a governança do Unity Catalog. Nenhuma cópia. |
| Gravar `request.json` **antes** do 202 | Se a App cair depois do aceite, o request não se perde. |
| Score **dentro da App**, com o `predict` do modelo registrado | Um salto a menos; o código de features é o mesmo que foi validado e registrado. |
| Observabilidade gravada **depois** da decisão | Um cold start do SQL warehouse nunca atrasa a resposta. |
| Status lido do Volume quando a instância não conhece o id | Funciona após restart e com várias instâncias da App. |
| Dependências do modelo fixadas nas versões do treino | Evita que a App instale uma scikit-learn diferente da que serializou o modelo. |
| Grants por `GRANT` nos notebooks, não no bundle | Grants no bundle são autoritativos e removeriam os que a App concede ao próprio service principal. |
| Dev sem `mode: development` | Esse modo prefixa o schema com o usuário, e o caminho do Volume faz parte do contrato com a AWS. |

## Trade-offs

| Ganho | Custo |
|---|---|
| 4 componentes AWS e o Model Serving saem do caminho | O Credit Engine troca a URL e passa a usar token OAuth |
| Um caminho só, de 5 KB a 64 MiB | Sem fila: um request interrompido depois do 202 fica `running` até ser reprocessado |
| Nada de cold start (a App fica ligada) | Custo fixo da App (tamanho MEDIUM; 2 instâncias em produção) |
| Decisão em ~3 s com 29 MB | Sem inference tables nativas; a observabilidade é a tabela própria |
| Mesmo response router | Ele passa a ler o `response_dbx.json` a partir do evento S3 |

## Segurança

- Credit Engine → App: OAuth M2M de um service principal com `CAN_USE` na App.
- App → Unity Catalog: o service principal da App tem só `WRITE_VOLUME` no Volume, `EXECUTE` no
  modelo, `MODIFY` na tabela de observabilidade e `CAN_USE` no warehouse.
- O corpo dos requests não é escrito em log; a tabela guarda só metadados e o ponteiro.
- Bucket privado, criptografado, com política de retenção.

## Próximos passos sugeridos

1. Registrar o bucket real como Volume EXTERNAL e apontar o modelo real (`src/model/features.py`).
2. Job de reprocessamento para requests sem `response_dbx.json` (proteção contra restart).
3. Recarga automática do Champion na App (hoje basta reiniciar).
4. Dashboard de comparação PEGA × Databricks para decidir o cutover.
