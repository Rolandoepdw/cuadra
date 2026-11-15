-- 019_traces.sql
-- Execution tracing: input/output of critical methods for debugging failures.

CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    method TEXT NOT NULL,
    input_json TEXT,
    output_json TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    error_type TEXT,
    duration_ms REAL,
    conversation_id INTEGER,
    turn_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_traces_conversation ON traces(conversation_id);
CREATE INDEX IF NOT EXISTS idx_traces_turn ON traces(turn_id);
CREATE INDEX IF NOT EXISTS idx_traces_method ON traces(method);
CREATE INDEX IF NOT EXISTS idx_traces_created_at ON traces(created_at);
CREATE INDEX IF NOT EXISTS idx_traces_ok ON traces(ok);
