"""Lambda simuladora: leitura do evento S3 que chega pela inbox."""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "aws"), str(ROOT / "src" / "common")]
os.environ.update({"AWS_DEFAULT_REGION": "us-east-1", "BUCKET": "bucket", "INBOX_URL": "https://sqs/inbox"})


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        with mock.patch("boto3.client"):
            self.sim = importlib.reload(importlib.import_module("credit_engine_sim"))

    def test_evento_s3_cru_e_envelope_sns(self):
        raw = json.dumps({"Records": [{"s3": {"object": {"key": "payloads/dt%3D2026-10-05/r1/response_dbx.json"}}}]})
        expected = ["payloads/dt=2026-10-05/r1/response_dbx.json"]
        self.assertEqual(self.sim.s3_keys(raw), expected)
        self.assertEqual(self.sim.s3_keys(json.dumps({"Message": raw})), expected)
        self.assertEqual(self.sim.s3_keys(json.dumps({"Event": "s3:TestEvent"})), [])


if __name__ == "__main__":
    unittest.main()
