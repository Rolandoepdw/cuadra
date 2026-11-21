-- =========================================================
-- 005: conversation identity (which Telegram account replies)
--
--   bot     = @cuadra_demo_bot (Bot API)
--   userbot = @cuadra_demo_userbot (Telethon)
--
-- The identity that received the last inbound message is used to reply from
-- the same account, so clients can talk to either one without duplicate or
-- mismatched senders. New conversations default to the userbot.
-- =========================================================

ALTER TABLE conversations
    ADD COLUMN identity TEXT NOT NULL DEFAULT 'userbot'
    CHECK (identity IN ('bot', 'userbot'));
