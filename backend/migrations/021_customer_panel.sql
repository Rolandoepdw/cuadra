-- =========================================================
-- 021: customer identity, contacts and duplicate signals (F2)
--
-- `customers` was a name plus a JSON bag. The Clients panel needs queryable
-- identity (phone, zone, address, status) and a place to keep every phone the
-- customer gave us. `preferences_json` stays readable for compatibility; new
-- writes go to these columns and `customer_contacts`.
--
-- Identity rule: a phone is never used to merge two customers automatically.
-- When a phone is already linked to another customer, `customer_contacts` keeps
-- the first owner and `customer_signals` raises `posible_duplicado` on both
-- sides for the operator to review.
--
-- Tags, notes, signals and merges are created here because they are part of the
-- same identity model; the Clients panel (F3) is what starts writing them.
-- =========================================================

ALTER TABLE customers ADD COLUMN display_name TEXT;
ALTER TABLE customers ADD COLUMN phone TEXT;
ALTER TABLE customers ADD COLUMN phone_normalized TEXT;
ALTER TABLE customers ADD COLUMN usual_zone_id INTEGER REFERENCES zones(id);
ALTER TABLE customers ADD COLUMN usual_address TEXT;
ALTER TABLE customers ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE', 'BLOCKED', 'SPAM', 'MERGED'));
ALTER TABLE customers ADD COLUMN merged_into_id INTEGER REFERENCES customers(id);
ALTER TABLE customers ADD COLUMN first_seen_at TEXT;
ALTER TABLE customers ADD COLUMN last_seen_at TEXT;

CREATE INDEX IF NOT EXISTS idx_customers_phone_normalized
    ON customers(phone_normalized);
CREATE INDEX IF NOT EXISTS idx_customers_last_seen ON customers(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);

CREATE TABLE IF NOT EXISTS customer_contacts (
    id            INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('phone', 'email', 'whatsapp')),
    value         TEXT NOT NULL,
    normalized    TEXT NOT NULL,
    label         TEXT,
    source        TEXT NOT NULL,          -- channel | sale | fact | operator
    confidence    REAL NOT NULL DEFAULT 1.0,
    verified      INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT DEFAULT (datetime('now')),
    last_seen_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (kind, normalized)
);
CREATE INDEX IF NOT EXISTS idx_customer_contacts_customer
    ON customer_contacts(customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_contacts_normalized
    ON customer_contacts(normalized);

CREATE TABLE IF NOT EXISTS customer_tags (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | auto
    reason      TEXT,
    created_by  TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (customer_id, tag)
);

CREATE TABLE IF NOT EXISTS customer_notes (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    author      TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS customer_signals (
    id             INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    severity       TEXT NOT NULL DEFAULT 'info'
                   CHECK (severity IN ('info', 'warning', 'critical')),
    status         TEXT NOT NULL DEFAULT 'OPEN'
                   CHECK (status IN ('OPEN', 'CONFIRMED', 'DISMISSED')),
    count          INTEGER NOT NULL DEFAULT 1,
    evidence_json  TEXT,
    first_seen_at  TEXT DEFAULT (datetime('now')),
    last_seen_at   TEXT DEFAULT (datetime('now')),
    resolved_at    TEXT,
    resolved_by    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_signals_open
    ON customer_signals(customer_id, kind) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_customer_signals_status
    ON customer_signals(status, severity);

CREATE TABLE IF NOT EXISTS customer_merges (
    id                INTEGER PRIMARY KEY,
    from_customer_id  INTEGER NOT NULL REFERENCES customers(id),
    into_customer_id  INTEGER NOT NULL REFERENCES customers(id),
    reason            TEXT,
    created_by        TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);
