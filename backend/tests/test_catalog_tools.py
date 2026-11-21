"""L3.1: catalog as a real multi-category read.

Throwaway SQLite database, no model server. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_catalog_tools -v
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cuadra import config, db
from cuadra.ai.tools import base
from cuadra.ai.tools import catalog as catalog_tools
from cuadra.domain import catalog


def _frame(conn):
    return SimpleNamespace(
        conn=conn,
        cache={},
        conversation={"id": 1},
        turn_id=None,
        evidence=[],
    )


class CatalogToolsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        self.conn = db.connect()
        db.apply_migrations(self.conn)
        self._seed()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _seed(self):
        conn = self.conn
        conn.execute(
            """INSERT INTO suppliers (id, name, telegram_id, delivery_zone)
               VALUES (1, 'TCP Uno', '111222333', 'La Habana')"""
        )
        conn.executemany(
            "INSERT INTO categories (id, name) VALUES (?, ?)",
            [
                (1, "tecnología"),
                (2, "computación"),
                (3, "accesorios"),
                (4, "hogar"),
                (5, "vacía"),
                (6, "motos_eléctricas"),
                (7, "motos_combustión"),
                (8, "cuatro"),
                (9, "seis"),
                (10, "doce"),
                (11, "agotables"),
            ],
        )
        rows = [
            # id, category, name, price, stock, reserved, status
            (1, 1, "Laptop Ultraligera", 720.0, 1, 0, "ACTIVE"),
            (2, 3, "Mouse Inalámbrico", 15.0, 25, 0, "ACTIVE"),
            (3, 3, "Teclado Mecánico", 45.0, 10, 0, "ACTIVE"),
            (4, 3, "Audífonos Bluetooth", 35.0, 8, 0, "ACTIVE"),
            (5, 4, "Freidora de Aire", 80.0, 5, 0, "ACTIVE"),
            (6, 6, "Moto Eléctrica Cargo", 1200.0, 2, 0, "ACTIVE"),
            (7, 7, "Moto Combustión 150", 900.0, 3, 0, "ACTIVE"),
            (8, 11, "Power Bank 10000", 25.0, 0, 0, "ACTIVE"),
            (9, 11, "Power Bank 20000", 35.0, 0, 0, "OUT_OF_STOCK"),
        ]
        for index in range(1, 13):
            rows.append((100 + index, 10, f"Producto Doce {index}", 10.0, index, 0, "ACTIVE"))
        for index in range(1, 7):
            rows.append((200 + index, 9, f"Producto Seis {index}", 10.0, index, 0, "ACTIVE"))
        for index in range(1, 5):
            rows.append((300 + index, 8, f"Producto Cuatro {index}", 10.0, index, 0, "ACTIVE"))
        conn.executemany(
            """INSERT INTO products
               (id, supplier_id, category_id, name, description, price,
                currency, commission, commission_currency, commission_type,
                stock, stock_reserved, status, specifications_json)
               VALUES (?, 1, ?, ?, 'Producto de prueba', ?, 'USD', 1.0, 'USD',
                       'FIXED', ?, ?, ?, '{"garantia": "3 meses"}')""",
            [
                (pid, category, name, price, stock, reserved, status)
                for pid, category, name, price, stock, reserved, status in rows
            ],
        )
        conn.commit()

    # ------------------------------------------------------------ migration
    def test_a_direct_insert_also_fills_the_bridge(self):
        conn = self.conn
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, category_id, name, price, currency, commission,
                stock, status)
               VALUES (500, 1, 1, 'Tablet Directa', 100, 'USD', 1, 2, 'ACTIVE')"""
        )
        self.assertEqual(catalog.product_category_names(conn, 500), ["tecnología"])

    def test_create_product_accepts_several_categories(self):
        product = catalog.create_product(
            self.conn,
            {
                "supplier_id": 1,
                "name": "Dock USB C",
                "price": 40,
                "commission": 2,
                "category_ids": [1, 2],
                "category_id": 2,
            },
        )
        self.assertEqual(product["category_id"], 2)
        self.assertEqual(product["categories"], ["computación", "tecnología"])
        self.assertEqual(catalog.product_category_ids(self.conn, product["id"]), [2, 1])

    def test_update_product_replaces_the_category_set(self):
        product = catalog.update_product(
            self.conn, 2, {"category_ids": [1, 3], "category_id": 1}
        )
        self.assertEqual(product["categories"], ["tecnología", "accesorios"])
        self.assertEqual(product["category_id"], 1)
        updated = catalog.update_product(self.conn, 2, {"category_ids": [3]})
        self.assertEqual(updated["categories"], ["accesorios"])
        self.assertEqual(updated["category_id"], 3)

    # -------------------------------------------------------------- ranking
    def test_accessories_for_a_laptop_do_not_list_the_laptop(self):
        found = catalog.search_catalog(self.conn, "accesorios para laptop")
        names = [product["name"] for product in found["products"]]
        self.assertEqual(
            set(names),
            {"Mouse Inalámbrico", "Teclado Mecánico", "Audífonos Bluetooth"},
        )
        self.assertNotIn("Laptop Ultraligera", names)
        self.assertEqual(found["match_kind"], "category")
        self.assertEqual(found["matched_categories"], ["accesorios"])

    def test_a_product_in_two_categories_appears_once(self):
        self.conn.execute(
            """INSERT INTO product_categories (product_id, category_id, is_primary)
               VALUES (6, 7, 0)"""
        )
        found = catalog.search_catalog(self.conn, "motos")
        ids = [product["id"] for product in found["products"]]
        self.assertEqual(ids.count(6), 1)
        self.assertEqual(set(ids), {6, 7})

    def test_a_qualifier_narrows_the_family(self):
        found = catalog.search_catalog(self.conn, "laptop ultraligera")
        self.assertEqual([product["id"] for product in found["products"]], [1])

    def test_a_qualifier_that_matches_nothing_does_not_drop_the_category(self):
        found = catalog.search_catalog(self.conn, "accesorios para laptop gamer")
        ids = {product["id"] for product in found["products"]}
        self.assertEqual(ids, {2, 3, 4})
        self.assertNotIn(1, ids)

    def test_four_products_return_four(self):
        found = catalog.search_catalog(self.conn, "cuatro")
        self.assertEqual(len(found["products"]), 4)

    def test_six_products_return_six(self):
        found = catalog.search_catalog(self.conn, "seis")
        self.assertEqual(len(found["products"]), 6)

    def test_twelve_products_are_capped_at_ten_without_filler(self):
        found = catalog.search_catalog(self.conn, "doce")
        self.assertEqual(len(found["products"]), 10)
        self.assertTrue(
            all(product["name"].startswith("Producto Doce") for product in found["products"])
        )

    def test_out_of_stock_is_included_and_marked(self):
        found = catalog.search_catalog(self.conn, "agotables")
        by_id = {product["id"]: product for product in found["products"]}
        self.assertEqual(set(by_id), {8, 9})
        self.assertEqual(by_id[8]["availability"], "out_of_stock")
        self.assertEqual(by_id[9]["availability"], "out_of_stock")

    def test_include_out_of_stock_false_hides_them(self):
        found = catalog.search_catalog(
            self.conn, "agotables", include_out_of_stock=False
        )
        self.assertEqual(found["products"], [])
        self.assertEqual(found["empty_reason"], "no_match")

    def test_unknown_category_is_no_category(self):
        found = catalog.search_catalog(self.conn, "comida")
        self.assertEqual(found["products"], [])
        self.assertEqual(found["empty_reason"], "no_category")

    def test_empty_category_is_category_empty(self):
        found = catalog.search_catalog(self.conn, "vacía")
        self.assertEqual(found["products"], [])
        self.assertEqual(found["empty_reason"], "category_empty")

    def test_a_whole_category_lists_every_real_product_and_nothing_else(self):
        found = catalog.search_catalog(self.conn, "accesorios")
        names = {product["name"] for product in found["products"]}
        self.assertEqual(
            names,
            {"Mouse Inalámbrico", "Teclado Mecánico", "Audífonos Bluetooth"},
        )

    def test_stock_only_breaks_ties(self):
        self.conn.execute("UPDATE products SET stock = 0 WHERE id = 2")
        self.conn.execute("UPDATE products SET stock = 5 WHERE id = 3")
        self.conn.commit()
        found = catalog.search_catalog(self.conn, "accesorios")
        ids = [product["id"] for product in found["products"]]
        # Teclado has stock; Mouse is out of stock. Both are still listed.
        self.assertLess(ids.index(3), ids.index(2))

    # ---------------------------------------------------------------- tools
    def test_tool_search_returns_the_public_contract(self):
        registry = base.Registry()
        catalog_tools.register(registry)
        result = registry.run(
            "catalog.search", {"query": "accesorios para laptop"}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.empty_reason, None)
        ids = {row["id"] for row in result.data["products"]}
        self.assertEqual(ids, {2, 3, 4})
        self.assertTrue(result.evidence)
        for row in result.data["products"]:
            self.assertEqual(
                set(row),
                {
                    "id",
                    "name",
                    "categories",
                    "price",
                    "currency",
                    "stock",
                    "status",
                    "availability",
                    "highlights",
                },
            )

    def test_tool_get_includes_categories_and_zones(self):
        registry = base.Registry()
        catalog_tools.register(registry)
        result = registry.run("catalog.get", {"product_id": 2}, _frame(self.conn))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["categories"], ["accesorios"])
        self.assertIn("zones", result.data)
        self.assertNotIn("commission", result.data)

    def test_tool_check_stock(self):
        registry = base.Registry()
        catalog_tools.register(registry)
        frame = _frame(self.conn)
        result = registry.run(
            "catalog.check_stock",
            {"product_id": 1, "quantity": 2},
            frame,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["available"])
        self.assertEqual(result.data["stock"], 1)

    def test_tool_list_category(self):
        registry = base.Registry()
        catalog_tools.register(registry)
        result = registry.run(
            "catalog.list_category", {"category": "accesorios"}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["products"]), 3)


if __name__ == "__main__":
    unittest.main()
