-- =========================================================
-- 017: customer follow-up state for stale sales
--
-- `sale_followup` v2 sends ONE reminder to the customer when a sale in
-- CONFIRMED or NEEDS_OPERATOR stops moving (doc 13 section 10.24).
--
-- `followup_sent_at`  one-shot guard, independent from `updated_at` (which must
--                     keep meaning "last status change" for the dashboard).
-- `followup_muted_at` operator opt-out for a single sale.
--
-- Additive and safe on existing rows: both stay NULL, so nothing is queued
-- until the operator enables the mode.
-- =========================================================

ALTER TABLE sales ADD COLUMN followup_sent_at TEXT;
ALTER TABLE sales ADD COLUMN followup_muted_at TEXT;
