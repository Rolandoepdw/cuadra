-- =========================================================
-- 012: observability, idempotency and turns (phases 0-1)
--
-- Three independent pieces land here because they share one migration:
--   llm_calls           every model call, with redacted prompts (phase 0)
--   messages.external_id  channel id for deduplication (phase 1)
--   turns               persisted debounce unit, one reply per burst (phase 1)
--
-- Traces never store raw phone numbers or addresses: `ai/redact.py` masks
-- them before insert and `redacted = 1` records that fact. Retention is a
-- setting (`llm.trace_retention_days`) enforced by the maintenance scheduler.
-- =========================================================

CREATE TABLE IF NOT EXISTS llm_calls (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT DEFAULT (datetime('now')),
    conversation_id  INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    job_id           INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    purpose          TEXT NOT NULL,          -- intent | compose | summary | tcp_extract | other
    model            TEXT NOT NULL,
    schema_name      TEXT,
    prompt_tokens    INTEGER,
    output_tokens    INTEGER,
    latency_ms       INTEGER,
    ok               INTEGER NOT NULL DEFAULT 1 CHECK (ok IN (0, 1)),
    error            TEXT,
    redacted         INTEGER NOT NULL DEFAULT 1 CHECK (redacted IN (0, 1)),
    prompt_text      TEXT,                   -- redacted JSON of the messages sent
    completion_text  TEXT                    -- redacted model output
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_purpose ON llm_calls(purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_conversation ON llm_calls(conversation_id, id DESC);

-- Channel-supplied id (Telegram update/chat message id). NULL for rows created
-- before this migration and for channels that do not expose one. The partial
-- index keeps duplicate inserts cheap while allowing many NULLs.
ALTER TABLE messages ADD COLUMN external_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external
    ON messages(conversation_id, external_id)
    WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS turns (
    id                INTEGER PRIMARY KEY,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id),
    status            TEXT NOT NULL DEFAULT 'OPEN'
                      CHECK (status IN ('OPEN', 'PROCESSING', 'DONE', 'CANCELLED')),
    first_message_id  INTEGER,
    last_message_id   INTEGER,
    message_ids_json  TEXT NOT NULL DEFAULT '[]',
    due_at            TEXT NOT NULL,
    job_id            INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_turns_due ON turns(status, due_at);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, id DESC);

INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES
    ('llm.trace_retention_days', '30', datetime('now')),
    ('llm.trace_prompts', '1', datetime('now'));
