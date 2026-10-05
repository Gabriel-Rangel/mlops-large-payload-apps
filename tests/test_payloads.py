"""Gerador de payload sintético: precisão do tamanho e formato do request."""

from __future__ import annotations

import json
import unittest

from src.common.payloads import SCALAR_FEATURES, make_bureau_payload, make_request_body, serialize


class PayloadGeneratorTests(unittest.TestCase):
    def test_tamanho_com_erro_menor_que_um_registro(self):
        for mb in (0.01, 15, 29, 32):
            raw = serialize(make_bureau_payload(mb))
            target = int(mb * 1024 * 1024)
            self.assertLessEqual(len(raw), target, mb)
            self.assertLess(target - len(raw), 256, mb)

    def test_historico_realista_e_nao_enchimento(self):
        doc = make_bureau_payload(15)
        self.assertGreater(len(doc["items"]), 50_000)
        self.assertTrue({"amount", "category", "date", "status"} <= set(doc["items"][0]))

    def test_deterministico(self):
        self.assertEqual(serialize(make_bureau_payload(0.5, seed=3)), serialize(make_bureau_payload(0.5, seed=3)))

    def test_request_completo(self):
        req = json.loads(make_request_body(1, request_id="abc"))
        self.assertEqual(req["request_id"], "abc")
        self.assertTrue(set(SCALAR_FEATURES) <= set(req))
        self.assertIsInstance(req["payload"]["items"], list)


if __name__ == "__main__":
    unittest.main()
