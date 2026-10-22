-- =========================================================
-- 005: conversation identity (which Telegram account replies)
--
--   bot     = @CuadraHabanaBot (Bot API)
--   userbot = @REPDCuadra (Telethon)
--
-- The identity that received the last inbound message is used to reply from
-- the same account, so clients can talk to either one without duplicate or
-- mismatched senders. New conversations default to the userbot.
-- =========================================================

ALTER TABLE conversations
    ADD COLUMN identity TEXT NOT NULL DEFAULT 'userbot'
    CHECK (identity IN ('bot', 'userbot'));
