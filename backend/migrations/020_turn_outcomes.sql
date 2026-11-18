-- =========================================================
-- 020: turn outcomes and customer kind
--
-- The Conversations panel needs, per turn, what the agent understood and what
-- it produced: intent, product, stage, generator, escalation, sale and latency.
-- Today that only lives in `traces`, which is a 30-day debugging log. These
-- columns make it queryable for the life of the conversation at the cost of
-- one UPDATE per turn.
--
-- `customers.kind` separates real customers from suppliers (TCP). TCP
-- conversations are stored as customers because they share the channel
-- identity, but they must not pollute customer metrics or segments.
--
-- All changes are additive; `preferences_json` and `customer_channels` stay
-- untouched.
-- =========================================================

ALTER TABLE customers ADD COLUMN kind TEXT NOT NULL DEFAULT 'customer'
    CHECK (kind IN ('customer', 'supplier'));

ALTER TABLE turns ADD COLUMN intent TEXT;
ALTER TABLE turns ADD COLUMN product_id INTEGER REFERENCES products(id);
ALTER TABLE turns ADD COLUMN stage TEXT;
ALTER TABLE turns ADD COLUMN generator TEXT;
ALTER TABLE turns ADD COLUMN escalated INTEGER NOT NULL DEFAULT 0
    CHECK (escalated IN (0, 1));
ALTER TABLE turns ADD COLUMN sale_id INTEGER REFERENCES sales(id);
ALTER TABLE turns ADD COLUMN reply_message_id INTEGER REFERENCES messages(id);
ALTER TABLE turns ADD COLUMN latency_ms INTEGER;
ALTER TABLE turns ADD COLUMN intent_ms INTEGER;
ALTER TABLE turns ADD COLUMN compose_ms INTEGER;
ALTER TABLE turns ADD COLUMN context_tokens INTEGER;
ALTER TABLE turns ADD COLUMN outcome TEXT NOT NULL DEFAULT 'ok'
    CHECK (outcome IN ('ok', 'error'));
ALTER TABLE turns ADD COLUMN error TEXT;
ALTER TABLE turns ADD COLUMN finished_at TEXT;

CREATE INDEX IF NOT EXISTS idx_turns_intent ON turns(intent);
CREATE INDEX IF NOT EXISTS idx_turns_product ON turns(product_id);
CREATE INDEX IF NOT EXISTS idx_turns_sale ON turns(sale_id);
CREATE INDEX IF NOT EXISTS idx_customers_kind ON customers(kind);

-- Existing TCP conversations become suppliers. Numeric peers are the normal
-- match; the handle is included for rows created before 016 learned the peer.
UPDATE customers
   SET kind = 'supplier'
 WHERE id IN (
        SELECT cc.customer_id
          FROM customer_channels cc
          JOIN suppliers s
            ON cc.identifier = s.telegram_peer_id
            OR cc.identifier = s.telegram_id
         WHERE cc.channel = 'telegram'
       );
