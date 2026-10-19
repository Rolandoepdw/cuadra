-- 001_init.sql — Cuadra initial schema.
-- SOURCE OF TRUTH for the schema; cuadra_doc/02_modelo_datos.md summarizes it.

-- =========================================================
-- CATEGORIES (operator-curated taxonomy)
-- =========================================================
CREATE TABLE categories (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    active  INTEGER DEFAULT 1
);

-- =========================================================
-- SUPPLIERS (TCP)
-- =========================================================
CREATE TABLE suppliers (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    business_alias    TEXT,
    whatsapp_phone    TEXT,
    telegram_id       TEXT,
    delivery_zone     TEXT,
    payment_terms     TEXT,
    priority          INTEGER DEFAULT 100,
    notes             TEXT,
    active            INTEGER DEFAULT 1,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

-- =========================================================
-- PRODUCTS (ONE supplier's listing; no canonical catalog)
-- =========================================================
CREATE TABLE products (
    id                   INTEGER PRIMARY KEY,
    supplier_id          INTEGER NOT NULL REFERENCES suppliers(id),
    category_id          INTEGER REFERENCES categories(id),
    name                 TEXT NOT NULL,
    description          TEXT,
    price                REAL NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'USD'
                         CHECK (currency IN ('USD', 'CUP')),
    commission           REAL NOT NULL,
    commission_currency  TEXT NOT NULL DEFAULT 'USD'
                         CHECK (commission_currency IN ('USD', 'CUP')),
    stock                INTEGER DEFAULT 1,
    status               TEXT NOT NULL DEFAULT 'ACTIVE'
                         CHECK (status IN ('ACTIVE', 'OUT_OF_STOCK', 'PAUSED', 'REMOVED')),
    specifications_json  TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT DEFAULT (datetime('now')),
    updated_at           TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_products_supplier ON products(supplier_id);
CREATE INDEX idx_products_status ON products(status);

CREATE TABLE product_images (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    local_path  TEXT NOT NULL,
    is_main     INTEGER DEFAULT 0,
    sort_order  INTEGER DEFAULT 0
);
CREATE INDEX idx_product_images_product ON product_images(product_id);

-- =========================================================
-- CUSTOMERS (multi-channel identity)
-- =========================================================
CREATE TABLE customers (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    preferences_json TEXT DEFAULT '{}',
    notes           TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE customer_channels (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    identifier  TEXT NOT NULL,
    UNIQUE(channel, identifier)
);

-- =========================================================
-- CONVERSATIONS AND MESSAGES
-- =========================================================
CREATE TABLE conversations (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    channel      TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    status       TEXT NOT NULL DEFAULT 'ACTIVE'
                 CHECK (status IN ('ACTIVE', 'WAITING_TCP', 'CLOSED')),
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_conversations_status ON conversations(status);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    direction       TEXT NOT NULL CHECK (direction IN ('INBOUND', 'OUTBOUND')),
    author          TEXT NOT NULL DEFAULT 'customer'
                    CHECK (author IN ('customer', 'bot', 'tcp', 'operator')),
    type            TEXT NOT NULL DEFAULT 'text'
                    CHECK (type IN ('text', 'audio', 'image', 'document')),
    body            TEXT,
    file_path       TEXT,
    latency_ms      INTEGER,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);

-- =========================================================
-- TCP ESCALATIONS (critical loop)
-- =========================================================
CREATE TABLE escalations (
    id                INTEGER PRIMARY KEY,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id),
    product_id        INTEGER REFERENCES products(id),
    supplier_id       INTEGER NOT NULL REFERENCES suppliers(id),
    question          TEXT NOT NULL,
    tcp_message_sent  TEXT,
    tcp_answer        TEXT,
    status            TEXT NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING', 'ANSWERED',
                                        'RESOLVED', 'CANCELLED', 'TIMEOUT')),
    attempts          INTEGER DEFAULT 0,
    created_at        TEXT DEFAULT (datetime('now')),
    answered_at       TEXT,
    resolved_at       TEXT
);
CREATE INDEX idx_escalations_status ON escalations(status);
CREATE INDEX idx_escalations_conversation ON escalations(conversation_id);

-- =========================================================
-- SALES AND COMMISSIONS
-- Flow: DRAFT -> PENDING_APPROVAL -> CONFIRMED -> PAID | CANCELLED
-- The operator ALWAYS closes the payment.
-- =========================================================
CREATE TABLE sales (
    id               INTEGER PRIMARY KEY,
    conversation_id  INTEGER REFERENCES conversations(id),
    product_id       INTEGER NOT NULL REFERENCES products(id),
    supplier_id      INTEGER NOT NULL REFERENCES suppliers(id),
    customer_id      INTEGER NOT NULL REFERENCES customers(id),
    amount           REAL NOT NULL,
    currency         TEXT NOT NULL CHECK (currency IN ('USD', 'CUP')),
    commission_type  TEXT NOT NULL DEFAULT 'FIXED'
                     CHECK (commission_type IN ('FIXED', 'PERCENT')),
    commission       REAL NOT NULL,
    context_json     TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'DRAFT'
                     CHECK (status IN ('DRAFT', 'PENDING_APPROVAL',
                                       'CONFIRMED', 'PAID', 'CANCELLED')),
    source           TEXT NOT NULL DEFAULT 'bot' CHECK (source IN ('bot', 'operator')),
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now')),
    approved_at      TEXT,
    paid_at          TEXT
);
CREATE INDEX idx_sales_status ON sales(status);
CREATE INDEX idx_sales_supplier ON sales(supplier_id);
CREATE INDEX idx_sales_customer ON sales(customer_id);

-- =========================================================
-- BROADCAST GROUPS (100% operator-curated)
-- =========================================================
CREATE TABLE broadcast_groups (
    id                INTEGER PRIMARY KEY,
    channel           TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    target            TEXT NOT NULL,
    name              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'ACTIVE'
                      CHECK (status IN ('ACTIVE', 'PAUSED', 'BLOCKED')),
    max_posts_per_day INTEGER DEFAULT 3,
    min_interval_min  INTEGER DEFAULT 10,
    max_interval_min  INTEGER DEFAULT 40,
    notes             TEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(channel, target)
);

-- Which categories may be published in each group (many-to-many)
CREATE TABLE group_categories (
    group_id    INTEGER NOT NULL REFERENCES broadcast_groups(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, category_id)
);

-- =========================================================
-- BROADCASTS (campaigns; targets are computed at send time)
-- =========================================================
CREATE TABLE broadcasts (
    id           INTEGER PRIMARY KEY,
    channel      TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    template     TEXT NOT NULL,
    product_id   INTEGER REFERENCES products(id),
    status       TEXT NOT NULL DEFAULT 'SCHEDULED'
                 CHECK (status IN ('SCHEDULED', 'SENDING', 'COMPLETED',
                                   'FAILED', 'CANCELLED')),
    next_send_at TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

-- Explicit manual group selection for a broadcast (optional)
CREATE TABLE broadcast_group_links (
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    group_id     INTEGER NOT NULL REFERENCES broadcast_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (broadcast_id, group_id)
);

-- Auditable log of each real post (what went out, where, when)
CREATE TABLE broadcast_posts (
    id          INTEGER PRIMARY KEY,
    broadcast_id INTEGER REFERENCES broadcasts(id),
    group_id    INTEGER NOT NULL REFERENCES broadcast_groups(id),
    channel     TEXT NOT NULL,
    sent_text   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'SENT'
                CHECK (status IN ('SENT', 'FAILED')),
    error       TEXT,
    sent_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_broadcast_posts_group ON broadcast_posts(group_id, sent_at);

-- =========================================================
-- JOBS (persistent queue; survives power cuts)
-- =========================================================
CREATE TABLE jobs (
    id             INTEGER PRIMARY KEY,
    type           TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    priority       INTEGER DEFAULT 100,
    status         TEXT NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
    attempts       INTEGER DEFAULT 0,
    max_attempts   INTEGER DEFAULT 3,
    next_retry_at  TEXT,
    last_error     TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_jobs_queue ON jobs(status, next_retry_at, priority);

-- =========================================================
-- EVENTS (system audit trail)
-- =========================================================
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    type         TEXT NOT NULL,
    entity       TEXT,
    entity_id    INTEGER,
    details_json TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_events_type ON events(type, created_at);
