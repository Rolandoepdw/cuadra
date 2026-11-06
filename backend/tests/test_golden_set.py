"""Guard rails for the phase 0 golden set (synthetic, no real data)."""

import json
import unittest
from pathlib import Path

from cuadra.ai.llm import INTENTS

GOLDEN = Path(__file__).resolve().parent / "golden"


def load_jsonl(name: str) -> list[dict]:
    with (GOLDEN / name).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class GoldenSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.products = json.loads((GOLDEN / "products.json").read_text(encoding="utf-8"))
        cls.nlu = load_jsonl("nlu.jsonl")
        cls.compose = load_jsonl("compose.jsonl")

    def test_has_enough_cases(self):
        self.assertGreaterEqual(len(self.nlu), 50)
        self.assertGreaterEqual(len(self.compose), 10)

    def test_ids_are_unique(self):
        for cases in (self.nlu, self.compose):
            ids = [case["id"] for case in cases]
            self.assertEqual(len(ids), len(set(ids)))

    def test_intents_and_products_are_valid(self):
        for case in self.nlu:
            self.assertIn(case["expect"], INTENTS, case["id"])
            if case.get("product"):
                self.assertIn(case["product"], self.products, case["id"])

    def test_compose_cases_declare_allowed_numbers(self):
        for case in self.compose:
            self.assertIn("allowed_numbers", case, case["id"])
            for name in case.get("candidates", []):
                self.assertIn(name, self.products, case["id"])


if __name__ == "__main__":
    unittest.main()
