"""App com Volume e modelo simulados: 29 MB aceitos, 413 acima do limite, 202 durável e status
recuperado do Volume depois que a memória da App é perdida."""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from src.common.payloads import make_request_body

APP_DIR = Path(__file__).resolve().parents[1] / "app"


class _FakeScorer:
    model_version = "7"
    model_alias = "Champion"

    def score(self, scalars, payload, request_id):
        return 1, 0.9


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(
            {
                "DATABRICKS_CATALOG": "cat",
                "DATABRICKS_SCHEMA": "sch",
                "PAYLOAD_VOLUME": "payloads",
                "MODEL_URI": "models:/cat.sch.m@Champion",
                "MAX_BODY_BYTES": str(40 * 1024 * 1024),
            }
        )
        sys.path.insert(0, str(APP_DIR))
        cls.app_module = importlib.import_module("app")
        cls.storage = importlib.import_module("storage")
        cls.job_store = importlib.import_module("job_store")

    def setUp(self):
        from fastapi.testclient import TestClient

        self.files: dict[str, bytes] = {}  # "Volume" em memória
        patches = [
            mock.patch.object(self.storage, "write_bytes", side_effect=lambda uri, raw: self.files.__setitem__(uri, raw)),
            mock.patch.object(self.storage, "read_json",
                              side_effect=lambda uri: json.loads(self.files[uri]) if uri in self.files else None),
            mock.patch.object(self.storage, "exists", side_effect=lambda uri: uri in self.files),
            mock.patch.object(self.storage, "insert_observability_row"),
            mock.patch.object(self.app_module.scorer_holder, "get", return_value=_FakeScorer()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(self.app_module.app)

    def test_post_de_29mb_duravel_e_pontuado(self):
        body = make_request_body(29)
        resp = self.client.post("/invocations", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 202, resp.text)
        accepted = resp.json()
        # O request.json tem exatamente os bytes enviados e foi gravado antes do 202.
        self.assertEqual(self.files[accepted["payload_uri"]], body)
        self.assertTrue(accepted["payload_uri"].startswith("/Volumes/cat/sch/payloads/dt="))
        done = self.client.get(f"/invocations/{accepted['request_id']}").json()
        self.assertEqual((done["status"], done["prediction"]), ("done", 1))
        self.assertEqual(json.loads(self.files[accepted["response_uri"]])["status"], "done")

    def test_acima_do_limite_e_413_sem_gravar(self):
        resp = self.client.post("/invocations", content=make_request_body(41), headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.files, {})

    def test_sem_payload_e_400(self):
        self.assertEqual(self.client.post("/invocations", json={f"var_{i:02d}": 0 for i in range(1, 16)}).status_code, 400)

    def test_status_sobrevive_a_restart(self):
        accepted = self.client.post("/invocations", content=make_request_body(0.01, request_id="pega-123")).json()
        self.assertEqual(accepted["request_id"], "pega-123")
        with self.job_store._lock:
            self.job_store._jobs.clear()  # simula restart / outra instância
        self.assertEqual(self.client.get("/invocations/pega-123").json()["status"], "done")
        del self.files[accepted["response_uri"]]
        self.assertEqual(self.client.get("/invocations/pega-123").json()["status"], "running")
        self.assertEqual(self.client.get("/invocations/desconhecido").status_code, 404)

    def test_request_id_invalido(self):
        self.assertEqual(self.client.post("/invocations", content=make_request_body(0.01, request_id="../etc")).status_code, 400)


if __name__ == "__main__":
    unittest.main()
