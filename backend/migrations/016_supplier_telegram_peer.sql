-- =========================================================
-- 016: supplier Telegram peer id (numeric)
--
-- `suppliers.telegram_id` stores the human handle (@usuario) used to reach the
-- TCP, but Telegram reports inbound messages with a numeric user id. Without
-- the numeric peer the inbound `RECIBIDO`/`ENTREGADO` acknowledgements are
-- ingested as customer messages and never correlate with a sale.
--
-- `dispatch_sale` resolves the handle with the userbot and stores the numeric
-- id here, so inbound TCP traffic can be matched without guessing.
-- =========================================================

ALTER TABLE suppliers
    ADD COLUMN telegram_peer_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_telegram_peer
    ON suppliers(telegram_peer_id)
    WHERE telegram_peer_id IS NOT NULL;
