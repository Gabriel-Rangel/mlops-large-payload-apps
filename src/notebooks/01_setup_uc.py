# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Setup do Unity Catalog
# MAGIC
# MAGIC O bundle já cria o schema e o Volume `payloads`. Este notebook cria a tabela
# MAGIC **`inference_observability`**: uma linha de metadados por request, com o ponteiro para o
# MAGIC `request.json` no Volume. O JSON completo nunca vai para tabelas (as inference tables do
# MAGIC Model Serving também não registram payloads acima de 1 MiB).

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {catalog}.{schema}.inference_observability (
      request_id        STRING    COMMENT 'Id do request (pode ser o id de correlação do Credit Engine)',
      event_ts          TIMESTAMP COMMENT 'Momento do registro (UTC)',
      endpoint_name     STRING    COMMENT 'Quem pontuou (nome da App)',
      model_version     STRING,
      model_alias       STRING    COMMENT 'Champion',
      status            STRING    COMMENT 'SUCCESS | ERROR',
      latency_ms        DOUBLE    COMMENT 'Igual a total_latency_ms',
      accept_latency_ms DOUBLE    COMMENT 'Do início do POST até o 202 (inclui gravar o request.json)',
      total_latency_ms  DOUBLE    COMMENT 'Do início do POST até a decisão',
      payload_uri       STRING    COMMENT 'Caminho do request.json no Volume',
      payload_hash      STRING    COMMENT 'sha256 do corpo original',
      payload_bytes     BIGINT,
      prediction        INT,
      probability       DOUBLE,
      error_message     STRING,
      env               STRING    COMMENT 'Catálogo do ambiente',
      ingested_at       TIMESTAMP,
      scalars_json      STRING    COMMENT 'As 15 variáveis (o JSON do birô fica só no Volume)'
    )
    COMMENT 'Observabilidade do score: metadados + ponteiro para o payload no Volume'
    """
)
display(spark.sql(f"SHOW TABLES IN {catalog}.{schema}"))
