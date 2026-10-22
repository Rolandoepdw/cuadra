-- =========================================================
-- 006: recurring broadcast cycles and second-based intervals
--
-- A campaign is no longer one-shot. Every `repeat_window_days` all active
-- campaigns are published `repeat_passes` times per target group, limited by
-- the group's daily cap and by the interval in seconds.
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
    min_interval_s    INTEGER DEFAULT 300,
    max_interval_s    INTEGER DEFAULT 1200,
    notes             TEXT,
    sender            TEXT NOT NULL DEFAULT 'userbot'
                      CHECK (sender IN ('bot', 'userbot')),
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(channel, target)
);

INSERT INTO broadcast_groups_new
    (id, channel, target, name, zone, status, max_posts_per_day,
     min_interval_s, max_interval_s, notes, sender, created_at, updated_at)
SELECT
    id, channel, target, name, zone, status, max_posts_per_day,
    COALESCE(min_interval_min, 5) * 60,
    COALESCE(max_interval_min, 20) * 60,
    notes, sender, created_at, updated_at
FROM broadcast_groups;

DROP TABLE broadcast_groups;
ALTER TABLE broadcast_groups_new RENAME TO broadcast_groups;

CREATE TABLE broadcast_posts_new (
    id           INTEGER PRIMARY KEY,
    broadcast_id INTEGER REFERENCES broadcasts(id),
    group_id     INTEGER NOT NULL REFERENCES broadcast_groups(id),
    channel      TEXT NOT NULL,
    sent_text    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'QUEUED'
                 CHECK (status IN ('QUEUED', 'SENT', 'FAILED')),
    error        TEXT,
    sent_at      TEXT DEFAULT (datetime('now')),
    image_path   TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    window_start TEXT
);

INSERT INTO broadcast_posts_new
    (id, broadcast_id, group_id, channel, sent_text, status, error,
     sent_at, image_path, attempts, window_start)
SELECT
    id, broadcast_id, group_id, channel, sent_text, status, error,
    sent_at, image_path, attempts, substr(sent_at, 1, 10)
FROM broadcast_posts;

DROP TABLE broadcast_posts;
ALTER TABLE broadcast_posts_new RENAME TO broadcast_posts;

CREATE INDEX idx_broadcast_posts_group ON broadcast_posts(group_id, sent_at);
CREATE INDEX idx_broadcast_posts_broadcast ON broadcast_posts(broadcast_id, status);
CREATE INDEX idx_broadcast_posts_cycle
    ON broadcast_posts(broadcast_id, group_id, sent_at);

UPDATE broadcasts
SET status = 'SCHEDULED', next_send_at = datetime('now'), updated_at = datetime('now')
WHERE status = 'COMPLETED';

COMMIT;
PRAGMA foreign_keys = ON;

INSERT INTO settings (key, value, updated_at) VALUES
    ('broadcast.default_max_posts_per_day', '20', datetime('now')),
    ('broadcast.default_min_interval_s', '300', datetime('now')),
    ('broadcast.default_max_interval_s', '1200', datetime('now')),
    ('broadcast.repeat_passes', '2', datetime('now')),
    ('broadcast.repeat_window_days', '1', datetime('now'))
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

DELETE FROM settings
WHERE key IN ('broadcast.default_min_interval_min', 'broadcast.default_max_interval_min');
