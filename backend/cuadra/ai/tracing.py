"""Persist LLM call traces (migration 012).

Every call to ``ai.llm.chat`` writes one row with model, purpose, latency,
tokens and a redacted copy of the prompt/completion. Tracing is best-effort:
a failure here must never break a customer reply, so every error is swallowed
and logged at debug level.

Prompts can be disabled with ``settings['llm.trace_prompts'] = '0'``; metadata
(model, latency, tokens, error) is still stored. Retention is controlled by
``settings['llm.trace_retention_days']`` (default 30) and enforced by
``purge_old`` from the maintenance scheduler.
"""

import logging

from cuadra import db
from cuadra.ai import redact
from cuadra.domain import settings

log = logging.getLogger("cuadra.tracing")

RETENTION_KEY = "llm.trace_retention_days"
PROMPTS_KEY = "llm.trace_prompts"

DEFAULT_RETENTION_DAYS = 30
MAX_ERROR_CHARS = 500


def _read_config(conn) -> tuple[bool, int]:
    """Return (store_prompts, retention_days) with safe fallbacks."""
    prompts = settings.get(conn, PROMPTS_KEY, "1") != "0"
    try:
        days = int(settings.get(conn, RETENTION_KEY, str(DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return prompts, max(1, min(days, 3650))


def record_call(
    *,
    purpose: str,
    model: str,
    schema_name: str | None = None,
    conversation_id: int | None = None,
    job_id: int | None = None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    ok: bool = True,
    error: str | None = None,
    messages: list[dict] | None = None,
    completion: str | None = None,
) -> None:
    """Insert one trace row. Never raises."""
    conn = None
    try:
        # Short busy timeout: a trace must never stall the reply on a busy DB.
        conn = db.connect(timeout=2)
        store_prompts, _ = _read_config(conn)
        prompt_text = redact.messages(messages) if store_prompts else None
        completion_text = redact.completion(completion) if store_prompts else None
        safe_error = redact.text(error)
        if safe_error and len(safe_error) > MAX_ERROR_CHARS:
            safe_error = safe_error[:MAX_ERROR_CHARS] + "..."
        conn.execute(
            """INSERT INTO llm_calls
                   (conversation_id, job_id, purpose, model, schema_name,
                    prompt_tokens, output_tokens, latency_ms, ok, error,
                    redacted, prompt_text, completion_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                conversation_id,
                job_id,
                purpose,
                model,
                schema_name,
                prompt_tokens,
                output_tokens,
                latency_ms,
                1 if ok else 0,
                safe_error,
                prompt_text,
                completion_text,
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — tracing is never allowed to break a reply
        log.debug("could not persist llm trace", exc_info=True)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if conn is not None:
            conn.close()


def purge_old(conn=None) -> int:
    """Delete traces past the configured retention window. Returns rows removed."""
    owns_connection = conn is None
    try:
        if owns_connection:
            conn = db.connect(timeout=5)
        _, days = _read_config(conn)
        cursor = conn.execute(
            "DELETE FROM llm_calls WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cursor.rowcount or 0
    except Exception:  # noqa: BLE001 — maintenance must not crash the scheduler
        log.debug("could not purge llm traces", exc_info=True)
        return 0
    finally:
        if owns_connection and conn is not None:
            conn.close()
