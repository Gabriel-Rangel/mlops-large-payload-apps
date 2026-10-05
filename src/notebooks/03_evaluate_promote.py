# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Gates e promoção (`@Challenger` → `@Champion`)
# MAGIC
# MAGIC Gates antes de promover:
# MAGIC 1. métrica mínima (`min_roc_auc`);
# MAGIC 2. signature registrada;
# MAGIC 3. smoke test do contrato: o mesmo request por `payload` inline (como a App chama) e por
# MAGIC    arquivo no Volume tem de dar o mesmo score.
# MAGIC
# MAGIC Com `promote_to_champion=true`, aponta o `@Champion` e concede ao service principal da App o
# MAGIC acesso ao modelo e à tabela de observabilidade. A App recarrega o Champion no restart.

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("model_name", "large_payload_scorer")
dbutils.widgets.text("payload_volume", "payloads")
dbutils.widgets.text("app_name", "")
dbutils.widgets.text("min_roc_auc", "0.70")
dbutils.widgets.dropdown("promote_to_champion", "false", ["false", "true"])

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
model_name = dbutils.widgets.get("model_name")
payload_volume = dbutils.widgets.get("payload_volume")
app_name = dbutils.widgets.get("app_name")
min_roc_auc = float(dbutils.widgets.get("min_roc_auc"))
promote = dbutils.widgets.get("promote_to_champion") == "true"
full_model_name = f"{catalog}.{schema}.{model_name}"

# COMMAND ----------

import json
from pathlib import Path

import mlflow
import pandas as pd
from mlflow.tracking import MlflowClient

mlflow.set_registry_uri("databricks-uc")
client = MlflowClient(registry_uri="databricks-uc")
version = client.get_model_version_by_alias(full_model_name, "Challenger").version
model_uri = f"models:/{full_model_name}/{version}"
print(f"Avaliando {full_model_name} v{version} (@Challenger)")

# Gate 1 — métrica registrada pelo treino.
roc_auc = float(client.get_model_version(full_model_name, version).tags.get("roc_auc", "0"))
assert roc_auc >= min_roc_auc, f"Gate de métrica: roc_auc {roc_auc} < {min_roc_auc}"

# Gate 2 — signature.
assert mlflow.models.get_model_info(model_uri).signature is not None, "Gate de signature: ausente"

# COMMAND ----------

# Gate 3 — smoke test do contrato. Jobs montam /Volumes via FUSE, então o arquivo é gravado direto.
scalars = {f"var_{i:02d}": 0.1 for i in range(1, 16)}
bureau = {"items": [{"amount": 10.5, "category": "A"}, {"amount": 3.0, "category": "B"}, {"amount": 7.2, "category": "A"}]}
smoke = Path(f"/Volumes/{catalog}/{schema}/{payload_volume}/_smoke/request.json")
smoke.parent.mkdir(parents=True, exist_ok=True)
smoke.write_text(json.dumps({**scalars, "payload": bureau}))

model = mlflow.pyfunc.load_model(model_uri)
by_file = model.predict(pd.DataFrame([{"file_path": str(smoke), "request_id": "smoke-file"}]))
by_inline = model.unwrap_python_model().predict(None, pd.DataFrame([{**scalars, "payload": bureau}]))
print(by_file, by_inline, sep="\n")
assert abs(float(by_file["probability"][0]) - float(by_inline["probability"][0])) < 1e-9, "inline e arquivo divergem"
print("Gates OK")

# COMMAND ----------

client.set_model_version_tag(full_model_name, version, "validation_status", "valid")
if not promote:
    dbutils.notebook.exit(f"Gates OK para v{version}; promote_to_champion=false, Champion inalterado")

client.set_registered_model_alias(full_model_name, "Champion", version)
print(f"@Champion → v{version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Acesso do service principal da App
# MAGIC
# MAGIC O modelo e a tabela de observabilidade são criados por jobs (depois do primeiro deploy do
# MAGIC bundle), então os grants ficam aqui. `GRANT` é aditivo e pode rodar a cada promoção.

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
app_sp = w.apps.get(app_name).service_principal_client_id
# Chamada REST direta: o tipo de `securable_type` no SDK muda entre versões (str ou enum).
w.api_client.do(
    "PATCH",
    f"/api/2.1/unity-catalog/permissions/function/{full_model_name}",  # modelos usam o tipo "function"
    body={"changes": [{"principal": app_sp, "add": ["EXECUTE"]}]},
)
for stmt in (
    f"GRANT USE CATALOG ON CATALOG {catalog} TO `{app_sp}`",
    f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{app_sp}`",
    f"GRANT SELECT, MODIFY ON TABLE {catalog}.{schema}.inference_observability TO `{app_sp}`",
):
    spark.sql(stmt)
print(f"Service principal da App ({app_sp}): EXECUTE no modelo, MODIFY na observabilidade")
