-- =========================================================
-- 004: broadcast identity, group zone and strategy defaults
--
-- `sender` now names the transport explicitly:
--   bot     = Bot API (kept dormant for future use)
--   userbot = Telethon account (default for broadcasts)
--
-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt.
-- `zone` is the municipality/area a group covers; it is informational today
-- and will drive product/group matching in the multi-zone phase.
-- =========================================================

PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE broadcast_groups_new (
    id                INTEGER PRIMARY KEY,
    channel           TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    target            TEXT NOT NULL,
    name              TEXT NOT NULL,
    zone              TEXT,
    status            TEXT NOT NULL DEFAULT 'ACTIVE'
                      CHECK (status IN ('ACTIVE', 'PAUSED', 'BLOCKED')),
    max_posts_per_day INTEGER DEFAULT 20,
    min_interval_min  INTEGER DEFAULT 5,
    max_interval_min  INTEGER DEFAULT 20,
    notes             TEXT,
    sender            TEXT NOT NULL DEFAULT 'userbot'
                      CHECK (sender IN ('bot', 'userbot')),
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(channel, target)
);

INSERT INTO broadcast_groups_new
    (id, channel, target, name, zone, status, max_posts_per_day,
     min_interval_min, max_interval_min, notes, sender, created_at, updated_at)
SELECT
    id, channel, target, name, NULL, status, max_posts_per_day,
    min_interval_min, max_interval_min, notes,
    CASE sender WHEN 'user' THEN 'userbot' ELSE sender END,
    created_at, updated_at
FROM broadcast_groups;

DROP TABLE broadcast_groups;
ALTER TABLE broadcast_groups_new RENAME TO broadcast_groups;

COMMIT;
PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES
    ('broadcast.default_max_posts_per_day', '20', datetime('now')),
    ('broadcast.default_min_interval_min', '5', datetime('now')),
    ('broadcast.default_max_interval_min', '20', datetime('now'));
