"""L3.0 grounding: a negative claim needs an empty read from this turn.

In `shadow` the report is recorded and the reply is not changed. These tests
pin the contract, not the shadow wiring.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_grounding -v
"""

import unittest
from types import SimpleNamespace

from cuadra.ai import grounding
from cuadra.ai.tools.base import ToolResult


def _frame(*, context=None, evidence=None):
    return SimpleNamespace(context=context or {}, evidence=evidence or [])


class NegativeClaimTests(unittest.TestCase):
    def test_negative_claims_are_detected_with_context(self):
        claims = grounding.negative_claims(
            "No hay laptops en este momento, pero puedo buscarte algo parecido."
        )
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].kind, "negative")

    def test_out_of_stock_counts_as_a_negative_claim(self):
        claims = grounding.negative_claims("Ese modelo está agotado.")
        self.assertEqual(len(claims), 1)

    def test_without_tool_evidence_the_check_is_not_measurable(self):
        report = grounding.validate_claims("No hay laptops", _frame(evidence=[]))
        self.assertTrue(report.ok)

    def test_an_empty_read_with_a_reason_grounds_the_claim(self):
        evidence = [
            ToolResult(ok=True, data=[], empty_reason="no_match", source="catalog.search")
        ]
        report = grounding.validate_claims(
            "No hay laptops", _frame(evidence=evidence)
        )
        self.assertTrue(report.ok)

    def test_a_negative_claim_without_an_empty_read_is_a_violation(self):
        evidence = [ToolResult(ok=True, data=[{"id": 1}], source="catalog.search")]
        report = grounding.validate_claims(
            "No hay laptops", _frame(evidence=evidence)
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].kind, "negative")
        self.assertEqual(
            report.violations[0].reason, "negative_without_empty_read"
        )

    def test_a_failed_read_does_not_ground_a_negative_claim(self):
        evidence = [
            ToolResult(
                ok=False, data=None, source="catalog.search",
                error=None,
            )
        ]
        report = grounding.validate_claims(
            "No hay laptops", _frame(evidence=evidence)
        )
        self.assertFalse(report.ok)


class PriceAndSpecTests(unittest.TestCase):
    def test_a_price_from_the_context_is_grounded(self):
        context = {"product": {"id": 1, "price": "12.50", "currency": "USD"}}
        report = grounding.validate_claims(
            "Cuesta 12.50 USD", _frame(context=context)
        )
        self.assertTrue(report.ok)

    def test_a_price_that_is_not_in_the_context_is_a_violation(self):
        context = {"product": {"id": 1, "price": "12.50", "currency": "USD"}}
        report = grounding.validate_claims("Cuesta 99 USD", _frame(context=context))
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].kind, "price")

    def test_a_spec_the_catalog_does_not_have_is_a_violation(self):
        context = {
            "product": {
                "id": 1,
                "name": "Power Bank",
                "price": "12.50",
                "specifications": {"memoria": "16GB"},
            }
        }
        report = grounding.validate_claims(
            "Tiene garantía de dos años", _frame(context=context)
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].kind, "specs")

    def test_the_report_is_json_safe(self):
        context = {"product": {"id": 1, "price": "12.50"}}
        report = grounding.validate_claims("Cuesta 99 USD", _frame(context=context))
        payload = report.as_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["violations"][0]["kind"], "price")
        self.assertIn("claim", payload["violations"][0])


if __name__ == "__main__":
    unittest.main()
