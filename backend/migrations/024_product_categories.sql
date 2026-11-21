-- =========================================================
-- 024: multi-category products
--
-- A product can belong to more than one category (a laptop can be
-- "tecnología" and "computación"; a charger can be "accesorios" and
-- "energía"). `products.category_id` stays as the primary category for
-- compatibility with the panel and every existing query.
--
-- Additive migration: nothing is deleted from `products`.
-- Rollback: DROP TABLE product_categories; restore the three FTS triggers
-- from 013 and reindex products_fts.
-- =========================================================

CREATE TABLE IF NOT EXISTS product_categories (
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, category_id)
);

CREATE INDEX IF NOT EXISTS idx_product_categories_category
    ON product_categories(category_id);

-- At most one primary category per product.
CREATE UNIQUE INDEX IF NOT EXISTS idx_product_categories_primary
    ON product_categories(product_id) WHERE is_primary = 1;

-- Backfill: the current category becomes the primary one.
INSERT OR IGNORE INTO product_categories (product_id, category_id, is_primary)
SELECT id, category_id, 1
FROM products
WHERE category_id IS NOT NULL;

-- ------------------------------------------------- keep the bridge in sync
-- Direct inserts (tests, imports, scripts) must not leave a product without
-- its primary category row.
CREATE TRIGGER IF NOT EXISTS trg_products_categories_ai
AFTER INSERT ON products
WHEN new.category_id IS NOT NULL
BEGIN
    INSERT OR IGNORE INTO product_categories (product_id, category_id, is_primary)
    VALUES (new.id, new.category_id, 1);
END;

CREATE TRIGGER IF NOT EXISTS trg_products_categories_au
AFTER UPDATE OF category_id ON products
WHEN new.category_id IS NOT NULL
BEGIN
    UPDATE product_categories SET is_primary = 0 WHERE product_id = new.id;
    INSERT OR IGNORE INTO product_categories (product_id, category_id, is_primary)
    VALUES (new.id, new.category_id, 1);
    UPDATE product_categories SET is_primary = 1
    WHERE product_id = new.id AND category_id = new.category_id;
END;

-- ------------------------------------------------------------- FTS layer
-- `category` in products_fts becomes every category name, not just the
-- primary one, so a search for "accesorios" finds a product filed under
-- "energía" that is also an accessory.
DROP TRIGGER IF EXISTS trg_products_fts_ai;
DROP TRIGGER IF EXISTS trg_products_fts_au;
DROP TRIGGER IF EXISTS trg_products_fts_ad;

CREATE TRIGGER trg_products_fts_ai AFTER INSERT ON products
BEGIN
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    VALUES (
        new.id,
        new.name,
        COALESCE(new.description, ''),
        COALESCE(
            (SELECT group_concat(c.name, ' ')
             FROM product_categories pc
             JOIN categories c ON c.id = pc.category_id
             WHERE pc.product_id = new.id),
            (SELECT name FROM categories WHERE id = new.category_id),
            ''
        ),
        COALESCE(new.specifications_json, '{}')
    );
END;

CREATE TRIGGER trg_products_fts_au AFTER UPDATE ON products
BEGIN
    DELETE FROM products_fts WHERE product_id = old.id;
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    VALUES (
        new.id,
        new.name,
        COALESCE(new.description, ''),
        COALESCE(
            (SELECT group_concat(c.name, ' ')
             FROM product_categories pc
             JOIN categories c ON c.id = pc.category_id
             WHERE pc.product_id = new.id),
            ''
        ),
        COALESCE(new.specifications_json, '{}')
    );
END;

CREATE TRIGGER trg_products_fts_ad AFTER DELETE ON products
BEGIN
    DELETE FROM products_fts WHERE product_id = old.id;
END;

-- Rebuild the row whenever the bridge changes.
CREATE TRIGGER IF NOT EXISTS trg_product_categories_fts_ai
AFTER INSERT ON product_categories
BEGIN
    DELETE FROM products_fts WHERE product_id = new.product_id;
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    SELECT p.id, p.name, COALESCE(p.description, ''),
           COALESCE(
               (SELECT group_concat(c.name, ' ')
                FROM product_categories pc
                JOIN categories c ON c.id = pc.category_id
                WHERE pc.product_id = p.id),
               ''
           ),
           COALESCE(p.specifications_json, '{}')
    FROM products p WHERE p.id = new.product_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_product_categories_fts_au
AFTER UPDATE ON product_categories
BEGIN
    DELETE FROM products_fts WHERE product_id = new.product_id;
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    SELECT p.id, p.name, COALESCE(p.description, ''),
           COALESCE(
               (SELECT group_concat(c.name, ' ')
                FROM product_categories pc
                JOIN categories c ON c.id = pc.category_id
                WHERE pc.product_id = p.id),
               ''
           ),
           COALESCE(p.specifications_json, '{}')
    FROM products p WHERE p.id = new.product_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_product_categories_fts_ad
AFTER DELETE ON product_categories
BEGIN
    DELETE FROM products_fts WHERE product_id = old.product_id;
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    SELECT p.id, p.name, COALESCE(p.description, ''),
           COALESCE(
               (SELECT group_concat(c.name, ' ')
                FROM product_categories pc
                JOIN categories c ON c.id = pc.category_id
                WHERE pc.product_id = p.id),
               ''
           ),
           COALESCE(p.specifications_json, '{}')
    FROM products p WHERE p.id = old.product_id;
END;

-- One-time reindex with the full category list.
DELETE FROM products_fts;
INSERT INTO products_fts (product_id, name, description, category, specifications)
SELECT p.id, p.name, COALESCE(p.description, ''),
       COALESCE(
           (SELECT group_concat(c.name, ' ')
            FROM product_categories pc
            JOIN categories c ON c.id = pc.category_id
            WHERE pc.product_id = p.id),
           ''
       ),
       COALESCE(p.specifications_json, '{}')
FROM products p;
