"""Phase 5: guard rails for the evaluation tools themselves.

These tests never call the model server; they only check that the comparison
helpers stay deterministic and that the golden set is still comparable.
"""

import importlib.util
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvalAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eval_agent = load_script("eval_agent")

    def test_product_ids_are_not_counted_as_invented_prices(self):
        class Response:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": "El Power Bank, ID 103, cuesta 20 USD."}}
                    ]
                }

            def raise_for_status(self):
                return None

        class Client:
            def post(self, *args, **kwargs):
                return Response()

        case = {
            "id": "x04",
            "text": "tienes 100 power bank guardadas, verdad?",
            "product": "powerbank",
            "allowed_numbers": [10000, 100, 20],
        }
        products = {"powerbank": {"id": 103, "nombre": "Power Bank 10000mAh"}}
        rows = self.eval_agent.evaluate_compose(Client(), [case], products, None)
        self.assertEqual(rows[0]["unknown_prices"], [])

    def test_prompt_fingerprint_is_short_and_stable(self):
        first = self.eval_agent.prompt_fingerprint()
        second = self.eval_agent.prompt_fingerprint()
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{16}$")

    def test_compare_reports_a_delta_per_metric(self):
        baseline = {"summary": {"intent_ok": 45, "latency_p95_ms": 3600, "leak_cases": 2}}
        current = {"summary": {"intent_ok": 51, "latency_p95_ms": 4800, "leak_cases": 0}}
        rows = {
            label: (before, after, delta)
            for label, before, after, delta in self.eval_agent.compare_summaries(
                current["summary"], baseline["summary"]
            )
        }
        self.assertEqual(rows["NLU intent ok"], (45, 51, "+6"))
        self.assertEqual(rows["leaks"], (2, 0, "-2"))
        self.assertEqual(rows["p95 ms"], (3600, 4800, "+1200"))

    def test_compare_skips_metrics_absent_from_both_reports(self):
        rows = self.eval_agent.compare_summaries({"intent_ok": 1}, {"intent_ok": 1})
        self.assertEqual([row[0] for row in rows], ["NLU intent ok"])

    def test_print_compare_reads_the_summary_block_of_full_reports(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.eval_agent.print_compare(
                {"summary": {"intent_ok": 51}, "prompt_fingerprint": "aaaa"},
                {"summary": {"intent_ok": 45}, "prompt_fingerprint": "bbbb"},
                current_tag="current",
                baseline_tag="baseline",
            )
        text = out.getvalue()
        self.assertIn("NLU intent ok", text)
        self.assertIn("45 -> 51", text)
        self.assertIn("prompt changed", text)


class EvalRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eval_retrieval = load_script("eval_retrieval")

    def test_product_signal_detection(self):
        products = {
            "cargador": {"nombre": "Cargador 20W USB-C"},
            "powerbank": {"nombre": "Power Bank 10000mAh"},
        }
        self.assertTrue(
            self.eval_retrieval._has_signal("tienes cargadores disponibles", products)
        )
        self.assertTrue(
            self.eval_retrieval._has_signal("y el power bank cuanto sale", products)
        )
        self.assertFalse(
            self.eval_retrieval._has_signal("lo quiero", products)
        )


if __name__ == "__main__":
    unittest.main()
