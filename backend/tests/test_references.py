"""L3.0 conversational references: ordinals, demonstratives, focus and ambiguity.

Resolution never guesses: when the frame cannot answer, the reference says so
and the policy has to ask.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_references -v
"""

import unittest
from types import SimpleNamespace

from cuadra.ai.turn.frame import Listing
from cuadra.ai.turn.references import resolve_reference


def _frame(*, ids=(), focus=None, context=None):
    return SimpleNamespace(
        last_listing=Listing(product_ids=list(ids)) if ids else None,
        focus_product=focus,
        context=context or {},
    )


class OrdinalTests(unittest.TestCase):
    def test_second_product_of_the_last_listing(self):
        reference = resolve_reference("me interesa el segundo", _frame(ids=[10, 20, 30]))
        self.assertEqual(reference.kind, "ordinal")
        self.assertEqual(reference.product_id, 20)
        self.assertFalse(reference.needs_clarification)

    def test_first_and_last_are_supported(self):
        frame = _frame(ids=[10, 20, 30])
        self.assertEqual(resolve_reference("el primero", frame).product_id, 10)
        self.assertEqual(resolve_reference("el último", frame).product_id, 30)

    def test_accents_do_not_break_the_ordinal(self):
        reference = resolve_reference("dame el ÚLTIMO", _frame(ids=[10, 20]))
        self.assertEqual(reference.product_id, 20)

    def test_ordinal_without_a_listing_asks_instead_of_guessing(self):
        reference = resolve_reference("el segundo", _frame())
        self.assertTrue(reference.needs_clarification)
        self.assertEqual(reference.reason, "no_listing")

    def test_ordinal_out_of_range_asks(self):
        reference = resolve_reference("el quinto", _frame(ids=[10, 20]))
        self.assertTrue(reference.needs_clarification)
        self.assertEqual(reference.reason, "out_of_range")


class DemonstrativeTests(unittest.TestCase):
    def test_esos_returns_the_whole_listing_in_order(self):
        reference = resolve_reference("esos me sirven", _frame(ids=[10, 20, 30]))
        self.assertEqual(reference.kind, "demonstrative")
        self.assertEqual(reference.product_ids, (10, 20, 30))
        self.assertIsNone(reference.product_id)

    def test_esos_dos_limits_the_selection(self):
        reference = resolve_reference("muéstrame esos dos", _frame(ids=[10, 20, 30]))
        self.assertEqual(reference.product_ids, (10, 20))

    def test_demonstrative_without_a_listing_asks(self):
        reference = resolve_reference("esos", _frame())
        self.assertTrue(reference.needs_clarification)


class FocusTests(unittest.TestCase):
    def test_el_mismo_uses_the_focus_product(self):
        reference = resolve_reference(
            "el mismo pero en azul", _frame(focus={"id": 42})
        )
        self.assertEqual(reference.kind, "focus")
        self.assertEqual(reference.product_id, 42)

    def test_el_de_antes_falls_back_to_the_persisted_context(self):
        reference = resolve_reference("el de antes", _frame(context={"product_id": 9}))
        self.assertEqual(reference.product_id, 9)

    def test_ambiguous_focus_asks(self):
        reference = resolve_reference("ese mismo", _frame(ids=[10, 20]))
        self.assertTrue(reference.needs_clarification)
        self.assertEqual(reference.reason, "ambiguous")


class NoReferenceTests(unittest.TestCase):
    def test_a_plain_question_has_no_reference(self):
        self.assertIsNone(
            resolve_reference("¿cuánto cuesta el power bank?", _frame(ids=[10]))
        )

    def test_empty_text_has_no_reference(self):
        self.assertIsNone(resolve_reference("", _frame(ids=[10])))


if __name__ == "__main__":
    unittest.main()
