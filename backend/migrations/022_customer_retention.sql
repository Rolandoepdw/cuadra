-- =========================================================
-- 022: customer panel settings (F4)
--
-- Thresholds already had code defaults in `domain/customer_panel.py`; this
-- migration seeds them into `settings` so the operator (and the future
-- Settings panel) can see and edit them without touching code.
--
-- `customers.message_retention_days` is the privacy policy: raw message bodies
-- older than 180 days (6 months) are purged by the scheduler. Customer profile,
-- sales and commissions are kept for accounting. `INSERT OR IGNORE` preserves
-- any value the operator already changed.
-- =========================================================

INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES
    ('customers.message_retention_days', '180', datetime('now')),
    ('customers.segment.vip_min_settled', '3', datetime('now')),
    ('customers.segment.vip_days', '90', datetime('now')),
    ('customers.segment.potential_days', '30', datetime('now')),
    ('customers.segment.cold_days', '90', datetime('now')),
    ('customers.segment.low_value_min_turns', '20', datetime('now')),
    ('customers.segment.low_value_min_conversations', '3', datetime('now')),
    ('customers.risk.medium', '30', datetime('now')),
    ('customers.risk.high', '60', datetime('now'));
