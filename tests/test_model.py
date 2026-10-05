"""Modelo: contrato file_path, leitura única do arquivo, hash e signature do MLflow."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from src.common.payloads import make_request_body, serialize
from src.model import features
from src.model.features import SCALAR_FEATURES, build_feature_frame, resolve_request, sha256_bytes

BUREAU = {"items": [{"amount": 10, "category": "A"}, {"amount": 5, "category": "B"}]}


class _StubClassifier:
    """Classificador fake: o teste é do contrato do PyFunc, não do modelo."""

    def predict_proba(self, X):
        import numpy as np

        p = 1 / (1 + np.exp(-X["json_total_amount"].to_numpy() / 100))
        return np.column_stack([1 - p, p])


class ResolveRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.full_path = root / "request.json"
        self.full_path.write_bytes(serialize({**{c: 0.5 for c in SCALAR_FEATURES}, "payload": BUREAU}))
        self.bureau_path = root / "bureau.json"
        self.bureau_path.write_bytes(serialize(BUREAU))

    def tearDown(self):
        self.tmp.cleanup()

    def test_so_file_path_le_variaveis_do_arquivo(self):
        scalars, bureau = resolve_request(pd.Series({"file_path": str(self.full_path)}))
        self.assertEqual(scalars["var_01"], 0.5)
        self.assertEqual(bureau, BUREAU)

    def test_variavel_do_request_tem_prioridade(self):
        scalars, _ = resolve_request(pd.Series({"file_path": str(self.full_path), "var_01": 9.0}))
        self.assertEqual((scalars["var_01"], scalars["var_02"]), (9.0, 0.5))

    def test_payload_uri_com_arquivo_so_do_biro(self):
        _, bureau = resolve_request(pd.Series({**{c: 0.1 for c in SCALAR_FEATURES}, "payload_uri": str(self.bureau_path)}))
        self.assertEqual(bureau, BUREAU)

    def test_variaveis_ausentes(self):
        with self.assertRaisesRegex(ValueError, "Variáveis ausentes"):
            resolve_request(pd.Series({"file_path": str(self.bureau_path)}))

    def test_sem_origem_do_json(self):
        with self.assertRaisesRegex(ValueError, "file_path"):
            resolve_request(pd.Series({c: 0.1 for c in SCALAR_FEATURES}))

    def test_hash_conferido_numa_leitura_so(self):
        raw = self.full_path.read_bytes()
        with mock.patch.object(features, "read_payload_bytes", wraps=features.read_payload_bytes) as reader:
            resolve_request(pd.Series({"file_path": str(self.full_path), "payload_hash": sha256_bytes(raw)}))
            self.assertEqual(reader.call_count, 1)
        with self.assertRaisesRegex(ValueError, "payload_hash divergente"):
            resolve_request(pd.Series({"file_path": str(self.full_path), "payload_hash": "sha256:" + "0" * 64}))

    def test_payload_inline_dict(self):
        frame = build_feature_frame(pd.DataFrame([{**{c: 0.0 for c in SCALAR_FEATURES}, "payload": BUREAU}]))
        self.assertEqual((frame["json_n_items"].iloc[0], frame["json_total_amount"].iloc[0]), (2.0, 15.0))

    def test_arquivo_de_29mb(self):
        path = Path(self.tmp.name) / "big.json"
        path.write_bytes(make_request_body(29))
        frame = build_feature_frame(pd.DataFrame([{"file_path": str(path)}]))
        self.assertGreater(frame["json_n_items"].iloc[0], 100_000)


class PyfuncSignatureTests(unittest.TestCase):
    def test_request_minimo_passa_na_signature(self):
        import joblib
        import mlflow

        from src.model.pyfunc_model import LargePayloadScorer, model_signature

        with tempfile.TemporaryDirectory() as tmp:
            # Tracking local e temporário: o teste não deixa arquivos do MLflow no repositório.
            os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{tmp}/mlflow.db"
            req = Path(tmp) / "request.json"
            req.write_text(json.dumps({**{c: 0.2 for c in SCALAR_FEATURES}, "payload": BUREAU}))
            artifact = Path(tmp) / "model.joblib"
            joblib.dump(_StubClassifier(), artifact)
            mlflow.pyfunc.save_model(
                path=str(Path(tmp) / "pyfunc"),
                python_model=LargePayloadScorer(),
                artifacts={"sklearn_model": str(artifact)},
                signature=model_signature(),
                code_paths=[str(Path(features.__file__).parent)],
            )
            loaded = mlflow.pyfunc.load_model(str(Path(tmp) / "pyfunc"))
            out = loaded.predict(pd.DataFrame([{"file_path": str(req), "request_id": "r1"}]))
            self.assertEqual(out["request_id"].iloc[0], "r1")
            self.assertIn(int(out["prediction"].iloc[0]), (0, 1))


if __name__ == "__main__":
    unittest.main()
