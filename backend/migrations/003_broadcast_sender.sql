-- Hito 7: per-group sending identity and auditable post metadata.
-- sender = 'bot' uses the Bot API; sender = 'user' uses the Telethon userbot.
ALTER TABLE broadcast_groups
    ADD COLUMN sender TEXT NOT NULL DEFAULT 'bot'
    CHECK (sender IN ('bot', 'user'));

ALTER TABLE broadcast_posts ADD COLUMN image_path TEXT;
ALTER TABLE broadcast_posts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_broadcast_posts_broadcast
    ON broadcast_posts(broadcast_id, status);

CREATE INDEX IF NOT EXISTS idx_broadcasts_due
    ON broadcasts(status, next_send_at);
