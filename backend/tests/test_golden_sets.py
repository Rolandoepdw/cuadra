"""L3.7: the golden sets for grounding and references stay green.

These are deterministic: no model server is needed. The planner's golden set is
validated in `test_tool_planning.py`.
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from cuadra.ai import grounding
from cuadra.ai.turn.frame import Listing
from cuadra.ai.turn.references import resolve_reference

GOLDEN = Path(__file__).resolve().parent / "golden"


def _rows(name: str) -> list[dict]:
    path = GOLDEN / name
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class GoldenGroundingTests(unittest.TestCase):
    def test_every_case_matches_its_expectation(self):
        for row in _rows("grounding.jsonl"):
            frame = SimpleNamespace(
                context=row.get("context") or {},
                evidence=row.get("evidence") or [],
            )
            report = grounding.validate_claims(row["reply"], frame)
            kinds = {violation.kind for violation in report.violations}
            expected = row.get("expect_violation")
            with self.subTest(case=row["id"]):
                if expected is None:
                    self.assertEqual(kinds, set(), row["reply"])
                else:
                    self.assertIn(expected, kinds, row["reply"])


class GoldenReferenceTests(unittest.TestCase):
    def test_every_case_matches_its_expectation(self):
        for row in _rows("references.jsonl"):
            frame = SimpleNamespace(
                last_listing=Listing(
                    product_ids=list(row.get("listing_ids") or [])
                ),
                focus_product=(
                    {"id": row["focus_id"]}
                    if row.get("focus_id") is not None
                    else None
                ),
                context={"product_id": row.get("context_id")},
            )
            reference = resolve_reference(row["text"], frame)
            expected_kind = row.get("expect_kind")
            with self.subTest(case=row["id"]):
                if expected_kind is None:
                    self.assertIsNone(reference)
                    continue
                self.assertIsNotNone(reference)
                self.assertEqual(reference.kind, expected_kind)
                if "expect_product_id" in row:
                    self.assertEqual(
                        reference.product_id, row["expect_product_id"]
                    )
                if "expect_product_ids" in row:
                    self.assertEqual(
                        list(reference.product_ids), row["expect_product_ids"]
                    )
                if "expect_clarification" in row:
                    self.assertEqual(
                        reference.needs_clarification,
                        row["expect_clarification"],
                    )


if __name__ == "__main__":
    unittest.main()
