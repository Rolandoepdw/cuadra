-- =========================================================
-- 014: autonomous sales, reserved stock and commission ledger
--
-- Three pieces land here:
--   products.stock_reserved / commission_type  physical vs reserved stock
--   sales                                      unified state machine
--   commission_ledger                          money Cuadra must collect
--
-- SQLite cannot alter a CHECK constraint in place, so `sales` is rebuilt with
-- the same pattern used by 006 and 011. The old PAID state becomes SETTLED:
-- the customer pays the TCP on delivery, and Cuadra collects its commission
-- afterwards.
--
-- The original design copied `commission_currency` from `sales`, but that
-- column never existed in 001; it is taken from the product listing instead so
-- historical rows keep their real currency.
-- =========================================================

-- ------------------------------------------------- physical vs reserved
ALTER TABLE products ADD COLUMN stock_reserved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN commission_type TEXT NOT NULL DEFAULT 'FIXED'
    CHECK (commission_type IN ('FIXED', 'PERCENT'));

-- --------------------------------------------------------- rebuild `sales`
PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE sales_new (
    id                  INTEGER PRIMARY KEY,
    order_code          TEXT,
    conversation_id     INTEGER REFERENCES conversations(id),
    product_id          INTEGER NOT NULL REFERENCES products(id),
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    customer_id         INTEGER NOT NULL REFERENCES customers(id),
    quantity            INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    amount              REAL NOT NULL CHECK (amount >= 0),
    currency            TEXT NOT NULL CHECK (currency IN ('USD', 'CUP')),
    commission_type     TEXT NOT NULL DEFAULT 'FIXED'
                        CHECK (commission_type IN ('FIXED', 'PERCENT')),
    commission          REAL NOT NULL,
    commission_currency TEXT NOT NULL DEFAULT 'USD'
                        CHECK (commission_currency IN ('USD', 'CUP')),
    recipient_name      TEXT,
    delivery_zone       TEXT,
    delivery_zone_id    INTEGER REFERENCES zones(id),
    delivery_address    TEXT,
    customer_phone      TEXT,
    status              TEXT NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN (
                            'DRAFT', 'PENDING_APPROVAL', 'PENDING_DISPATCH',
                            'DISPATCHED', 'CONFIRMED', 'DELIVERED',
                            'SETTLED', 'CANCELLED', 'NEEDS_OPERATOR'
                        )),
    source              TEXT NOT NULL DEFAULT 'bot'
                        CHECK (source IN ('bot', 'operator')),
    context_json        TEXT NOT NULL DEFAULT '{}',
    notes               TEXT,
    tcp_message_id      TEXT,
    approved_at         TEXT,
    dispatched_at       TEXT,
    tcp_ack_at          TEXT,
    delivered_at        TEXT,
    settled_at          TEXT,
    cancelled_at        TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

INSERT INTO sales_new (
    id, conversation_id, product_id, supplier_id, customer_id,
    amount, currency, commission_type, commission, commission_currency,
    context_json, status, source, approved_at, settled_at,
    created_at, updated_at
)
SELECT
    s.id, s.conversation_id, s.product_id, s.supplier_id, s.customer_id,
    s.amount, s.currency, s.commission_type, s.commission,
    COALESCE(
        (SELECT p.commission_currency FROM products p WHERE p.id = s.product_id),
        'USD'
    ),
    s.context_json,
    CASE s.status WHEN 'PAID' THEN 'SETTLED' ELSE s.status END,
    s.source, s.approved_at, s.paid_at,
    s.created_at, s.updated_at
FROM sales s;

DROP TABLE sales;
ALTER TABLE sales_new RENAME TO sales;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_order_code
    ON sales(order_code) WHERE order_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status);
CREATE INDEX IF NOT EXISTS idx_sales_supplier ON sales(supplier_id);
CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
CREATE INDEX IF NOT EXISTS idx_sales_conversation ON sales(conversation_id);

COMMIT;
PRAGMA foreign_keys = ON;

-- -------------------------------------------------------- commission ledger
CREATE TABLE IF NOT EXISTS commission_ledger (
    id                  INTEGER PRIMARY KEY,
    sale_id             INTEGER NOT NULL UNIQUE REFERENCES sales(id),
    product_id          INTEGER NOT NULL REFERENCES products(id),
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    product_name        TEXT NOT NULL,
    sale_total          REAL NOT NULL,
    currency            TEXT NOT NULL CHECK (currency IN ('USD', 'CUP')),
    commission_type     TEXT NOT NULL CHECK (commission_type IN ('FIXED', 'PERCENT')),
    commission_rate     REAL NOT NULL,
    commission_earned   REAL NOT NULL,
    commission_currency TEXT NOT NULL CHECK (commission_currency IN ('USD', 'CUP')),
    supplier_payout     REAL,   -- NULL when the currencies do not match
    status              TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'COLLECTED', 'VOID')),
    collected_at        TEXT,
    notes               TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON commission_ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_supplier ON commission_ledger(supplier_id);

-- Backfill the ledger for sales created before this migration. PAID/SETTLED
-- rows are considered already collected.
INSERT OR IGNORE INTO commission_ledger (
    sale_id, product_id, supplier_id, product_name,
    sale_total, currency, commission_type, commission_rate,
    commission_earned, commission_currency, supplier_payout,
    status, collected_at
)
SELECT
    s.id, s.product_id, s.supplier_id, p.name,
    s.amount, s.currency, s.commission_type, s.commission,
    s.commission, s.commission_currency,
    CASE WHEN s.commission_currency = s.currency
         THEN s.amount - s.commission ELSE NULL END,
    CASE WHEN s.status = 'SETTLED' THEN 'COLLECTED' ELSE 'PENDING' END,
    CASE WHEN s.status = 'SETTLED' THEN s.settled_at ELSE NULL END
FROM sales s
LEFT JOIN products p ON p.id = s.product_id
WHERE s.status != 'CANCELLED';

-- ------------------------------------------------- autonomy configuration
-- D23 (2026-11-19): a fresh database defaults to autonomous sales so the
-- operator does not have to configure the Finance panel after a reset. The
-- panel can still switch back to assisted/hybrid.
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('sales.mode', 'autonomous'),
    ('sales.hybrid_max_age_hours', '24'),
    ('catalog.autoapply.price', '1'),
    ('catalog.autoapply.stock', '1'),
    ('catalog.autoapply.specs', '1'),
    ('catalog.price_max_delta_pct', '50'),
    ('catalog.autoapply.min_confidence', '0.85');
