-- =========================================================
-- 023: per-product delivery zones
--
-- A supplier may cover a municipality but not send a small or low-margin item
-- there. `supplier_delivery_zones` keeps the supplier-wide coverage; this table
-- lets each product narrow it.
--
-- Semantics: no rows for a product means "inherit the supplier's zones", so the
-- migration is additive and every existing product keeps working unchanged.
-- When rows exist they are intersected with the supplier's coverage: the bot
-- never promises a zone the supplier does not reach.
-- =========================================================

CREATE TABLE product_delivery_zones (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    zone_id    INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, zone_id)
);
