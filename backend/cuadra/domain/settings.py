"""Mutable runtime settings (key/value), editable from the dashboard.

Distinct from `config` (env-driven, fixed at boot): these values live in the DB
so the operator can change them (e.g. the active LLM model) without editing
.env or restarting the process.
"""

from datetime import datetime, timezone

LLM_MODEL_KEY = "llm.model"
LLM_CTX_KEY = "llm.ctx"


def _now() -> str:
    # Match the schema's `datetime('now')` (UTC, no offset).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get(conn, key: str, default: str | None = None) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    except Exception:  # noqa: BLE001 — table may not exist yet (pre-migration)
        return default
    return row["value"] if row is not None else default


def set(conn, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value, _now()),
    )
    conn.commit()


def all(conn) -> dict[str, str | None]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
