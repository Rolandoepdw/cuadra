-- =========================================================
-- 013: memory, hybrid retrieval and idle conversation closing
--
-- Four independent pieces land here because they share one migration:
--   conversation_facts     current value per fact key (phase 2 memory)
--   products_fts           lexical layer for product retrieval (BM25)
--   product_embeddings     optional semantic layer (float32 vectors)
--   zone_aliases           colloquial zone names -> zones catalog
--   conversations.closed_at  idle closing timestamp
--
-- FTS5 and embeddings are optional at runtime: the bot degrades to the fuzzy
-- search in `domain/catalog.py`. The schema still exists so a fresh install
-- never needs a second migration when the model is downloaded.
-- =========================================================

-- ---------------------------------------------------------------- memory
CREATE TABLE IF NOT EXISTS conversation_facts (
    id                 INTEGER PRIMARY KEY,
    conversation_id    INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    fact_key           TEXT NOT NULL,
    fact_value         TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 1.0,
    source_message_id  INTEGER REFERENCES messages(id),
    updated_at         TEXT DEFAULT (datetime('now')),
    UNIQUE (conversation_id, fact_key) ON CONFLICT REPLACE
);
CREATE INDEX IF NOT EXISTS idx_conversation_facts_conversation
    ON conversation_facts(conversation_id);

-- ---------------------------------------------------------- zone aliases
-- `alias` is stored normalised (lowercase, no accents) by the Python layer.
-- The canonical name is also seeded as an alias so a freshly created zone is
-- immediately resolvable without inventing any hardcoded municipality list.
CREATE TABLE IF NOT EXISTS zone_aliases (
    alias   TEXT PRIMARY KEY,
    zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_zone_aliases_zone ON zone_aliases(zone_id);

INSERT OR IGNORE INTO zone_aliases (alias, zone_id)
SELECT lower(trim(name)), id
FROM zones
WHERE name IS NOT NULL AND trim(name) <> '';

-- --------------------------------------------------------- lexical layer
CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
    product_id UNINDEXED,
    name,
    description,
    category,
    specifications,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Backfill existing products; without this the index would be born empty.
INSERT INTO products_fts (product_id, name, description, category, specifications)
SELECT
    p.id,
    p.name,
    COALESCE(p.description, ''),
    COALESCE(c.name, ''),
    COALESCE(p.specifications_json, '{}')
FROM products p
LEFT JOIN categories c ON c.id = p.category_id;

-- Delete + insert is safer than UPDATE on FTS5. The delete trigger rarely runs
-- because products are soft-deleted (status='REMOVED'), but keeping it makes
-- the index consistent if a row is ever purged.
CREATE TRIGGER IF NOT EXISTS trg_products_fts_ai AFTER INSERT ON products
BEGIN
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    VALUES (
        new.id,
        new.name,
        COALESCE(new.description, ''),
        COALESCE((SELECT name FROM categories WHERE id = new.category_id), ''),
        COALESCE(new.specifications_json, '{}')
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_products_fts_au AFTER UPDATE ON products
BEGIN
    DELETE FROM products_fts WHERE product_id = old.id;
    INSERT INTO products_fts (product_id, name, description, category, specifications)
    VALUES (
        new.id,
        new.name,
        COALESCE(new.description, ''),
        COALESCE((SELECT name FROM categories WHERE id = new.category_id), ''),
        COALESCE(new.specifications_json, '{}')
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_products_fts_ad AFTER DELETE ON products
BEGIN
    DELETE FROM products_fts WHERE product_id = old.id;
END;

-- -------------------------------------------------------- semantic layer
-- The vector is float32 little-endian; `text_hash` avoids recomputing when the
-- product text did not change. The model name is part of the row so switching
-- models only invalidates rows that need it.
CREATE TABLE IF NOT EXISTS product_embeddings (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    text_hash  TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_product_embeddings_model
    ON product_embeddings(model);

-- ------------------------------------------------------ idle closing
ALTER TABLE conversations ADD COLUMN closed_at TEXT;
