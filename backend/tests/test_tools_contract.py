"""L3.0 tool contract: closed registry, typed args, stable errors, per-turn cache.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_tools_contract -v
"""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from cuadra import config, db
from cuadra.ai.tools import base
from cuadra.ai.tools import catalog as catalog_tools


class _Args(BaseModel):
    value: int = 1


def _frame(conn):
    return SimpleNamespace(
        conn=conn,
        cache={},
        conversation={"id": 1},
        turn_id=None,
        evidence=[],
    )


class RegistryContractTests(unittest.TestCase):
    def setUp(self):
        self.registry = base.Registry()

    def test_unknown_tool_is_forbidden_and_never_touches_the_domain(self):
        result = self.registry.run("catalog.unknown", {}, _frame(None))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, base.FORBIDDEN)
        self.assertIsNone(result.data)

    def test_write_tools_are_registered_but_never_callable(self):
        called = []

        def write_fn(conn, value=1):
            called.append(value)
            return {"id": 1}

        self.registry.register(
            base.ToolSpec(
                name="sales.create",
                domain="sales",
                description="creates a sale",
                input_model=_Args,
                permission=base.WRITE,
            ),
            write_fn,
        )
        result = self.registry.run("sales.create", {"value": 2}, _frame(None))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, base.FORBIDDEN)
        self.assertEqual(called, [])

    def test_invalid_arguments_return_a_stable_code(self):
        self.registry.register(
            base.ToolSpec(
                name="demo.read",
                domain="demo",
                description="reads",
                input_model=_Args,
            ),
            lambda conn, value=1: {"id": value},
        )
        result = self.registry.run("demo.read", {"value": "not-an-int"}, _frame(None))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, base.INVALID_ARGS)
        self.assertIn("errors", result.error.details)

    def test_a_slow_read_is_a_timeout_not_a_slow_customer(self):
        def slow(conn, value=1):
            time.sleep(0.02)
            return {"id": value}

        self.registry.register(
            base.ToolSpec(
                name="demo.slow",
                domain="demo",
                description="slow read",
                input_model=_Args,
                timeout_s=0.001,
            ),
            slow,
        )
        result = self.registry.run("demo.slow", {}, _frame(None))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, base.TIMEOUT)

    def test_reads_are_cached_inside_the_turn(self):
        calls = []

        def read(conn, value=1):
            calls.append(value)
            return {"id": value}

        self.registry.register(
            base.ToolSpec(
                name="demo.read",
                domain="demo",
                description="reads",
                input_model=_Args,
            ),
            read,
        )
        frame = _frame(None)
        first = self.registry.run("demo.read", {"value": 3}, frame)
        second = self.registry.run("demo.read", {"value": 3}, frame)
        self.assertTrue(first.ok)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(calls, [3])
        self.assertEqual(first.data, second.data)

    def test_invalidate_drops_the_turn_cache_after_a_write(self):
        self.registry.register(
            base.ToolSpec(
                name="demo.read",
                domain="demo",
                description="reads",
                input_model=_Args,
            ),
            lambda conn, value=1: {"id": value},
        )
        frame = _frame(None)
        self.registry.run("demo.read", {}, frame)
        self.registry.invalidate(frame)
        self.assertEqual(frame.cache, {})

    def test_modes_expose_reads_only_and_off_hides_everything(self):
        self.registry.register(
            base.ToolSpec(
                name="demo.read",
                domain="demo",
                description="reads",
                permission=base.READ,
            ),
            lambda conn: {},
        )
        self.registry.register(
            base.ToolSpec(
                name="demo.write",
                domain="demo",
                description="writes",
                permission=base.WRITE,
            ),
            lambda conn: {},
        )
        self.assertEqual(self.registry.allowed("off"), [])
        for mode in ("shadow", "on"):
            names = {spec.name for spec in self.registry.allowed(mode)}
            self.assertEqual(names, {"demo.read"})


class CatalogToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        self.registry = base.Registry()
        catalog_tools.register(self.registry)
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute(
                """INSERT INTO suppliers (id, name, delivery_zone)
                   VALUES (1, 'TCP Uno', 'La Habana')"""
            )
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock,
                    status, description)
                   VALUES (1, 1, 'Power Bank 10000', 12.5, 'USD', 2, 5,
                           'ACTIVE', 'Bateria 10000 mAh')"""
            )
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock,
                    status)
                   VALUES (2, 1, 'Power Bank 20000', 20, 'USD', 2, 0,
                           'OUT_OF_STOCK')"""
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _conn(self):
        return db.connect()

    def test_search_returns_evidence_and_includes_out_of_stock(self):
        conn = self._conn()
        try:
            result = self.registry.run("catalog.search", {"query": "power bank"}, _frame(conn))
        finally:
            conn.close()
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "catalog.search")
        ids = {row["id"] for row in result.data["products"]}
        self.assertIn(1, ids)
        self.assertIn(2, ids)
        self.assertTrue(result.evidence)

    def test_search_without_matches_is_ok_with_an_empty_reason(self):
        conn = self._conn()
        try:
            result = self.registry.run("catalog.search", {"query": "zzzz"}, _frame(conn))
        finally:
            conn.close()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["products"], [])
        self.assertEqual(result.empty_reason, "no_category")

    def test_get_missing_product_is_not_found(self):
        conn = self._conn()
        try:
            result = self.registry.run("catalog.get", {"product_id": 999}, _frame(conn))
        finally:
            conn.close()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, base.NOT_FOUND)

    def test_get_never_leaks_internal_economics(self):
        conn = self._conn()
        try:
            result = self.registry.run("catalog.get", {"product_id": 1}, _frame(conn))
        finally:
            conn.close()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["id"], 1)
        for key in ("commission", "cost", "margin"):
            self.assertNotIn(key, result.data)

    def test_catalog_tools_register_the_three_reads(self):
        registry = base.Registry()
        catalog_tools.register(registry)
        names = {spec.name for spec in registry.specs()}
        self.assertEqual(
            names,
            {
                "catalog.search",
                "catalog.get",
                "catalog.list_category",
                "catalog.check_stock",
            },
        )
        self.assertTrue(all(spec.permission == base.READ for spec in registry.specs()))


if __name__ == "__main__":
    unittest.main()
