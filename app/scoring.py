"""Score dentro da App, sem chamar o Model Serving.

Usa o `predict` do próprio PyFunc registrado no Unity Catalog: a engenharia de features vem
do código `model.features` logado com aquela versão do modelo. O payload é passado como dict,
sem serializar os 29 MB de novo.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import pandas as pd

logger = logging.getLogger("large-payload-app.scoring")

SCALAR_FEATURES = [f"var_{i:02d}" for i in range(1, 16)]


def extract_scalars(body: dict[str, Any]) -> dict[str, float]:
    missing = [c for c in SCALAR_FEATURES if c not in body]
    if missing:
        raise ValueError(f"Variáveis ausentes: {missing}")
    return {c: float(body[c]) for c in SCALAR_FEATURES}


class LocalScorer:
    """Carrega o modelo uma vez (ex.: models:/cat.schema.modelo@Champion) e pontua no processo."""

    def __init__(self, model_uri: str):
        import mlflow
        from mlflow.tracking import MlflowClient

        os.environ.setdefault("MLFLOW_TRACKING_URI", "databricks")
        mlflow.set_registry_uri("databricks-uc")
        self.model_uri = model_uri
        self.model_alias = model_uri.split("@", 1)[-1] if "@" in model_uri else None
        logger.info("Carregando %s", model_uri)
        self.python_model = mlflow.pyfunc.load_model(model_uri).unwrap_python_model()
        self.model_version: str | None = None
        if self.model_alias:
            name = model_uri.split("models:/", 1)[-1].split("@", 1)[0]
            self.model_version = str(
                MlflowClient(registry_uri="databricks-uc").get_model_version_by_alias(name, self.model_alias).version
            )

    def score(self, scalars: dict[str, float], payload: dict[str, Any], request_id: str) -> tuple[int, float]:
        out = self.python_model.predict(None, pd.DataFrame([{**scalars, "payload": payload, "request_id": request_id}]))
        return int(out["prediction"].iloc[0]), float(out["probability"].iloc[0])


class ScorerHolder:
    """Carregamento preguiçoso com nova tentativa a cada 30 s: a App sobe mesmo sem Champion ainda."""

    def __init__(self, model_uri: str, retry_s: float = 30.0):
        self.model_uri = model_uri
        self.retry_s = retry_s
        self.scorer: LocalScorer | None = None
        self.error: str | None = None
        self._last_attempt = 0.0
        self._lock = threading.Lock()

    def get(self) -> LocalScorer | None:
        if self.scorer is not None:
            return self.scorer
        with self._lock:
            if self.scorer is None and time.time() - self._last_attempt >= self.retry_s:
                self._last_attempt = time.time()
                try:
                    self.scorer = LocalScorer(self.model_uri)
                    self.error = None
                except Exception as exc:  # noqa: BLE001
                    self.error = str(exc)
                    logger.exception("Falha ao carregar %s; nova tentativa em %.0f s", self.model_uri, self.retry_s)
        return self.scorer
