-- Operational category for the audit trail. The dashboard Logs view groups
-- events into four windows without mapping every event type in SQL:
--   publications  sending/scheduling of campaigns
--   conversations bot, LLM and STT activity (replies, escalations, TCP flow)
--   entities      CRUD on catalog, groups, campaigns and strategy
--   system        job queue, rate limits, startup and LLM management
--
-- `events.log()` computes the category for every new row; this migration adds
-- the column and backfills the historical rows with the same rules.

ALTER TABLE events ADD COLUMN category TEXT NOT NULL DEFAULT 'system';

CREATE INDEX IF NOT EXISTS idx_events_category ON events(category, created_at DESC);

UPDATE events
SET category = CASE
    WHEN type IN (
        'broadcast_scheduled',
        'broadcast_posted',
        'broadcast_post_failed',
        'broadcast_no_targets',
        'broadcast_cycle_complete',
        'broadcast_send_now',
        'broadcasts_reevaluated'
    ) THEN 'publications'
    WHEN type IN (
        'reply_generated',
        'message_sent',
        'escalation_created',
        'escalation_sent',
        'escalation_resolved',
        'escalation_retry',
        'escalation_timeout',
        'tcp_message_received',
        'tcp_reply_unmatched',
        'nlu_invalid_output',
        'summary_updated',
        'audio_transcribed'
    ) OR source IN ('bot', 'llm', 'stt') THEN 'conversations'
    WHEN type LIKE '%\_created' ESCAPE '\'
      OR type LIKE '%\_updated' ESCAPE '\'
      OR type LIKE '%\_deleted' ESCAPE '\'
      OR type LIKE '%\_cancelled' ESCAPE '\'
      OR type LIKE '%\_applied' ESCAPE '\'
      OR type LIKE '%\_saved' ESCAPE '\'
      OR entity IN (
          'categories',
          'zones',
          'suppliers',
          'products',
          'product_images',
          'broadcast_groups',
          'broadcasts',
          'settings'
      )
    THEN 'entities'
    ELSE 'system'
END;
