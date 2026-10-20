-- =========================================================
-- SETTINGS (mutable runtime key/value config)
-- Lets the dashboard change things (e.g. the active LLM model)
-- without editing .env or restarting the process.
-- =========================================================
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
