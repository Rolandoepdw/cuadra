-- Phase 4: TCP catalog mutations, audit trail and reversible undo.
--
-- One row per TCP-originated change. A retried queue job must never duplicate a
-- row, hence the partial unique index on `source_message_id`. `notes` carries
-- the review reasons (out-of-range price, ambiguous correlation, MLC, ...).

CREATE TABLE IF NOT EXISTS catalog_mutation_log (
    id                  INTEGER PRIMARY KEY,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    escalation_id       INTEGER REFERENCES escalations(id),
    sale_id             INTEGER REFERENCES sales(id),
    source_message_id   INTEGER REFERENCES messages(id),
    source_text         TEXT,
    mutation_type       TEXT NOT NULL
                        CHECK (mutation_type IN ('PRICE', 'STOCK', 'STATUS',
                                                 'SPECS', 'ZONE', 'MIXED')),
    previous_state_json TEXT NOT NULL,
    new_state_json      TEXT NOT NULL,
    previous_updated_at TEXT,
    confidence          REAL NOT NULL DEFAULT 0.0,
    correlation_method  TEXT NOT NULL DEFAULT 'manual'
                        CHECK (correlation_method IN ('case_id', 'single_pending',
                                                      'heuristic', 'manual')),
    ambiguous           INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
    status              TEXT NOT NULL DEFAULT 'APPLIED'
                        CHECK (status IN ('APPLIED', 'PENDING_REVIEW',
                                          'REVERTED', 'REJECTED')),
    notes               TEXT,
    applied_by          TEXT NOT NULL DEFAULT 'bot'
                        CHECK (applied_by IN ('bot', 'operator')),
    reverted_at         TEXT,
    reverted_by         TEXT,
    revert_reason       TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mutation_source
    ON catalog_mutation_log(source_message_id)
    WHERE source_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mutation_status
    ON catalog_mutation_log(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mutation_product
    ON catalog_mutation_log(product_id, created_at DESC);
