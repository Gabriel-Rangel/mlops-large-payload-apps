# Código do modelo e notebooks

## `model/` — o modelo

| Arquivo | Papel |
|---|---|
| `features.py` | variáveis escalares, features do JSON do birô e leitura do payload (inline ou arquivo) |
| `pyfunc_model.py` | `LargePayloadScorer` (MLflow PyFunc) e a signature, com todas as colunas opcionais |

Esta pasta é registrada **junto com o modelo** (`code_paths`). Por isso a App pontua com o mesmo
código que foi validado nos gates. Para o modelo real, troque `SCALAR_FEATURES` e
`derive_features_from_json` e treine com os dados reais no notebook 02.

## `notebooks/` — ciclo de vida (executados pelos jobs do bundle)

| Notebook | Job | O que faz |
|---|---|---|
| `01_setup_uc.py` | `setup_uc` | cria a tabela `inference_observability` |
| `02_train_register.py` | `train_register` | treina, registra no Unity Catalog e aponta `@Challenger` |
| `03_evaluate_promote.py` | `evaluate_promote` | gates (métrica, signature, smoke test) e promoção para `@Champion`; dá acesso ao service principal da App |
| `04_validate_app.py` | `validate_app` | POST de 0,01 a 32 MB na App, SLO, arquivos no Volume, observabilidade e 413 |

## `common/payloads.py`

Gerador de JSON de birô sintético com tamanho controlado (muitos registros de histórico, como o
documento real), usado nos testes, no notebook 04 e na Lambda simuladora.
