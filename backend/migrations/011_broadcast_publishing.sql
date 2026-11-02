-- =========================================================
-- 011: broadcast publishing controls
--
-- Two independent, reversible switches replace the terminal CANCELLED state:
--   settings['broadcast.publishing']  global play/pause (1 = publishing)
--   broadcasts.enabled                per-campaign publish toggle
--
-- CANCELLED rows migrate to a disabled campaign parked until it is enabled
-- again, so the operation is reversible. SQLite cannot alter a CHECK
-- constraint in place, so `broadcasts` is rebuilt without CANCELLED.
-- =========================================================

PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE broadcasts_new (
    id           INTEGER PRIMARY KEY,
    channel      TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    template     TEXT NOT NULL,
    product_id   INTEGER REFERENCES products(id),
    status       TEXT NOT NULL DEFAULT 'SCHEDULED'
                 CHECK (status IN ('SCHEDULED', 'SENDING', 'COMPLETED', 'FAILED')),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    next_send_at TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

INSERT INTO broadcasts_new
    (id, channel, template, product_id, status, enabled, next_send_at,
     created_at, updated_at)
SELECT
    id, channel, template, product_id,
    CASE status WHEN 'CANCELLED' THEN 'SCHEDULED' ELSE status END,
    CASE status WHEN 'CANCELLED' THEN 0 ELSE 1 END,
    CASE status WHEN 'CANCELLED' THEN NULL ELSE next_send_at END,
    created_at, updated_at
FROM broadcasts;

DROP TABLE broadcasts;
ALTER TABLE broadcasts_new RENAME TO broadcasts;

CREATE INDEX IF NOT EXISTS idx_broadcasts_due
    ON broadcasts(status, next_send_at);

-- A campaign migrated from CANCELLED is disabled: anything it still had queued
-- must not survive. Its pending job fails cleanly in the worker (post missing).
DELETE FROM broadcast_posts
WHERE status = 'QUEUED'
  AND broadcast_id IN (SELECT id FROM broadcasts WHERE enabled = 0);

COMMIT;
PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO settings (key, value, updated_at)
VALUES ('broadcast.publishing', '1', datetime('now'));
