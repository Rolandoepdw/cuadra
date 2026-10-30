-- Telegram userbot anti-flood safety.
--
-- `peer_id` caches the canonical Telethon id of a group once it has been
-- resolved (joined or already a member), so sending never has to call
-- ImportChatInviteRequest again for the same target.
--
-- `sender_state` persists a per-account cooldown (FloodWait / PeerFlood) and
-- the last action time (human-like global gap) across restarts.
--
-- `rate_limit_hits` keeps rate-limit retries separate from ordinary retries:
-- a FloodWait must not consume the transient failure budget.

ALTER TABLE broadcast_groups ADD COLUMN peer_id INTEGER;

CREATE TABLE sender_state (
    sender         TEXT PRIMARY KEY,
    last_action_at TEXT,
    blocked_until  TEXT,
    reason         TEXT,
    updated_at     TEXT
);

ALTER TABLE jobs ADD COLUMN rate_limit_hits INTEGER DEFAULT 0;
