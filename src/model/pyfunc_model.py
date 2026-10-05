"""Modelo MLflow PyFunc: variáveis escalares + JSON do birô (inline ou por caminho no Volume).

Contrato (cada item de `dataframe_records`), todas as colunas opcionais:
  - file_path        : caminho /Volumes/... (contrato do cliente); `payload_uri` é sinônimo
  - var_01 .. var_15 : números; podem ser omitidos quando o arquivo traz o request completo
  - payload          : JSON inline (só para requests pequenos: limite de 16 MB no Model Serving)
  - payload_hash     : sha256 opcional dos bytes do arquivo (divergente → HTTP 400)
  - request_id       : id de correlação, devolvido na resposta

Request mínimo: {"dataframe_records": [{"file_path": "/Volumes/.../request.json"}]}
"""

from __future__ import annotations

from typing import Any

import mlflow
import pandas as pd
from mlflow.models import ModelSignature
from mlflow.types.schema import ColSpec, Schema

try:  # o import muda conforme o ambiente (notebook, Model Serving com code_paths, testes)
    from .features import OPTIONAL_REQUEST_COLUMNS, SCALAR_FEATURES, build_feature_frame  # type: ignore[import-not-found]
except ImportError:
    try:
        from model.features import OPTIONAL_REQUEST_COLUMNS, SCALAR_FEATURES, build_feature_frame  # type: ignore[no-redef]
    except ImportError:
        from features import OPTIONAL_REQUEST_COLUMNS, SCALAR_FEATURES, build_feature_frame  # type: ignore[no-redef]


class LargePayloadScorer(mlflow.pyfunc.PythonModel):
    """Envolve o classificador sklearn; o JSON é resolvido no predict (inline ou arquivo)."""

    def load_context(self, context: mlflow.pyfunc.PythonModelContext) -> None:
        import joblib

        self.model = joblib.load(context.artifacts["sklearn_model"])

    def predict(self, context, model_input: pd.DataFrame, params: dict[str, Any] | None = None):
        if not isinstance(model_input, pd.DataFrame):
            model_input = pd.DataFrame(model_input)
        # Cada arquivo é lido uma vez; hash ou variáveis inválidas levantam ValueError (HTTP 400).
        features = build_feature_frame(model_input)
        proba = self.model.predict_proba(features)[:, 1]
        return pd.DataFrame(
            {
                "prediction": (proba >= 0.5).astype(int),
                "probability": proba,
                "request_id": model_input["request_id"] if "request_id" in model_input.columns else [None] * len(model_input),
            }
        )


def model_signature() -> ModelSignature:
    # Tudo opcional: {"file_path": ...} sozinho é válido quando o arquivo traz o request completo.
    inputs = Schema(
        [ColSpec("double", c, required=False) for c in SCALAR_FEATURES]
        + [ColSpec("string", c, required=False) for c in OPTIONAL_REQUEST_COLUMNS]
    )
    outputs = Schema([ColSpec("long", "prediction"), ColSpec("double", "probability"), ColSpec("string", "request_id")])
    return ModelSignature(inputs=inputs, outputs=outputs)
