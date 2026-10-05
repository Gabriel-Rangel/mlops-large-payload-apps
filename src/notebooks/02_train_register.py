# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Treino e registro no Unity Catalog (`@Challenger`)
# MAGIC
# MAGIC Treina o classificador de demonstração, empacota como **PyFunc** (pasta `src/model`
# MAGIC vai junto, via `code_paths`), registra no Unity Catalog e aponta o alias **Challenger**.
# MAGIC
# MAGIC Para o modelo real: troque o gerador sintético pelos datasets de treino (registre-os com
# MAGIC `mlflow.log_input`) e as features em `src/model/features.py`.

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("model_name", "large_payload_scorer")
dbutils.widgets.text("experiment_path", "")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
model_name = dbutils.widgets.get("model_name")
experiment_path = dbutils.widgets.get("experiment_path")
full_model_name = f"{catalog}.{schema}.{model_name}"

# COMMAND ----------

import sys
from pathlib import Path

import cloudpickle
import joblib
import mlflow
import numpy as np
import pandas as pd
import sklearn
from mlflow.tracking import MlflowClient
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

# A raiz do repositório é sincronizada pelo bundle; procura a partir da pasta do notebook.
root = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src" / "model" / "features.py").exists())
# Import como `model.*` (e não `src.model.*`): é assim que o Model Serving recarrega a classe a
# partir de code_paths, que o MLflow coloca no sys.path.
sys.path.insert(0, str(root / "src"))
from model.features import make_synthetic_training_frame  # noqa: E402
from model.pyfunc_model import LargePayloadScorer, model_signature  # noqa: E402

mlflow.set_registry_uri("databricks-uc")
# O MLflow não cria pastas intermediárias: garante a pasta do experimento (ex.: /Shared/<bundle>).
from databricks.sdk import WorkspaceClient  # noqa: E402

WorkspaceClient().workspace.mkdirs(str(Path(experiment_path).parent))
mlflow.set_experiment(experiment_path)
print("Modelo:", full_model_name, "| Experimento:", experiment_path)

# COMMAND ----------

X, y = make_synthetic_training_frame(n=600, seed=42)
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42, stratify=y)

with mlflow.start_run(run_name="train-large-payload-scorer") as run:
    clf = LogisticRegression(max_iter=500).fit(X_train, y_train)
    auc = float(roc_auc_score(y_test, clf.predict_proba(X_test)[:, 1]))
    mlflow.log_metric("roc_auc", auc)
    mlflow.log_params({"algorithm": "LogisticRegression", "n_train": len(X_train), "n_test": len(X_test)})

    model_path = Path("/tmp/large_payload_model/model.joblib")
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, model_path)

    info = mlflow.pyfunc.log_model(
        name="model",
        python_model=LargePayloadScorer(),
        artifacts={"sklearn_model": str(model_path)},
        signature=model_signature(),
        registered_model_name=full_model_name,
        code_paths=[str(root / "src" / "model")],
        # Versões exatas do ambiente de treino: quem serve o modelo instala as mesmas
        # (sem pin, a scikit-learn instalada pode ser mais nova que a que serializou o modelo).
        pip_requirements=[
            f"mlflow=={mlflow.__version__}",
            f"scikit-learn=={sklearn.__version__}",
            f"pandas=={pd.__version__}",
            f"numpy=={np.__version__}",
            f"joblib=={joblib.__version__}",
            f"cloudpickle=={cloudpickle.__version__}",
            "databricks-sdk>=0.40.0",  # Files API: ler /Volumes de dentro do Model Serving
        ],
    )

version = info.registered_model_version
client = MlflowClient(registry_uri="databricks-uc")
client.set_registered_model_alias(full_model_name, "Challenger", version)
client.set_model_version_tag(full_model_name, version, "roc_auc", f"{auc:.4f}")
client.set_model_version_tag(full_model_name, version, "training_run_id", run.info.run_id)
print(f"Registrado {full_model_name} v{version} (roc_auc={auc:.4f}) → @Challenger")
dbutils.jobs.taskValues.set(key="model_version", value=str(version))
