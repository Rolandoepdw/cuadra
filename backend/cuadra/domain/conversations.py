"""Customers, conversations and messages (multi-channel identity).

The natural identity key is `(channel, identifier)` in `customer_channels`.
One open conversation per customer+channel; the rolling summary lives in
`conversations.context_json.summary` so it survives restarts.
"""

import json
import sqlite3
from datetime import datetime, timezone

from cuadra import config
from cuadra.domain.errors import NotFound

OPEN_STATUSES = ("ACTIVE", "WAITING_TCP")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_customer(
    conn, channel: str, identifier: str, name: str | None = None
) -> dict:
    row = conn.execute(
        """SELECT c.* FROM customers c
           JOIN customer_channels cc ON cc.customer_id = c.id
           WHERE cc.channel = ? AND cc.identifier = ?""",
        (channel, identifier),
    ).fetchone()
    if row is not None:
        return dict(row)
    cur = conn.execute("INSERT INTO customers (name) VALUES (?)", (name,))
    customer_id = cur.lastrowid
    conn.execute(
        "INSERT INTO customer_channels (customer_id, channel, identifier) VALUES (?, ?, ?)",
        (customer_id, channel, identifier),
    )
    conn.commit()
    return get_customer(conn, customer_id)


def get_customer(conn, customer_id: int) -> dict:
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise NotFound(f"customer {customer_id} not found")
    return dict(row)


def get_channel_identifier(conn, customer_id: int, channel: str) -> str | None:
    """The address to reply to on a channel (e.g. Telegram chat id)."""
    row = conn.execute(
        "SELECT identifier FROM customer_channels WHERE customer_id = ? AND channel = ?",
        (customer_id, channel),
    ).fetchone()
    return row["identifier"] if row is not None else None


def get_or_create_open_conversation(
    conn, customer_id: int, channel: str, identity: str | None = None
) -> dict:
    placeholders = ", ".join("?" for _ in OPEN_STATUSES)
    row = conn.execute(
        f"""SELECT * FROM conversations
            WHERE customer_id = ? AND channel = ? AND status IN ({placeholders})
            ORDER BY id DESC LIMIT 1""",
        (customer_id, channel, *OPEN_STATUSES),
    ).fetchone()
    if row is not None:
        if identity is not None and row["identity"] != identity:
            conn.execute(
                "UPDATE conversations SET identity = ?, updated_at = ? WHERE id = ?",
                (identity, _now(), row["id"]),
            )
            conn.commit()
            return get_conversation(conn, row["id"])
        return dict(row)
    cur = conn.execute(
        "INSERT INTO conversations (customer_id, channel, identity) VALUES (?, ?, ?)",
        (customer_id, channel, identity or "userbot"),
    )
    conn.commit()
    return get_conversation(conn, cur.lastrowid)


def get_conversation(conn, conversation_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"conversation {conversation_id} not found")
    return dict(row)


def get_context(conn, conversation_id: int) -> dict:
    column = conn.execute(
        "SELECT context_json FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if column is None:
        raise NotFound(f"conversation {conversation_id} not found")
    return json.loads(column["context_json"] or "{}")


def set_context(conn, conversation_id: int, context: dict) -> None:
    conn.execute(
        "UPDATE conversations SET context_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(context, ensure_ascii=False, separators=(",", ":")), _now(), conversation_id),
    )
    conn.commit()


def close_idle(conn, hours: float | None = None) -> int:
    """Close conversations with no messages for `hours` (default 72).

    A returning customer gets a new conversation, while `customers` and the
    profile survive. Open turns are cancelled first so an old debounce job can
    never answer after the conversation was closed.
    """
    idle_hours = config.CONVERSATION_IDLE_HOURS if hours is None else float(hours)
    modifier = f"-{idle_hours:g} hours"
    ids = [
        int(row["id"])
        for row in conn.execute(
            """SELECT id FROM conversations
               WHERE status IN ('ACTIVE', 'WAITING_TCP')
                 AND datetime(updated_at) < datetime('now', ?)""",
            (modifier,),
        )
    ]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    conn.execute(
        f"""UPDATE conversations
            SET status = 'CLOSED', closed_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id IN ({placeholders})""",
        ids,
    )
    conn.execute(
        f"""UPDATE turns
            SET status = 'CANCELLED', updated_at = datetime('now')
            WHERE status = 'OPEN' AND conversation_id IN ({placeholders})""",
        ids,
    )
    conn.commit()
    return len(ids)


def add_message(
    conn,
    conversation_id: int,
    direction: str,
    author: str,
    *,
    type: str = "text",
    body: str | None = None,
    file_path: str | None = None,
    latency_ms: int | None = None,
    external_id: str | None = None,
) -> dict:
    message, _ = add_message_with_status(
        conn,
        conversation_id,
        direction,
        author,
        type=type,
        body=body,
        file_path=file_path,
        latency_ms=latency_ms,
        external_id=external_id,
    )
    return message


def add_message_with_status(
    conn,
    conversation_id: int,
    direction: str,
    author: str,
    *,
    type: str = "text",
    body: str | None = None,
    file_path: str | None = None,
    latency_ms: int | None = None,
    external_id: str | None = None,
) -> tuple[dict, bool]:
    """Insert a message and report whether it was new.

    ``external_id`` is the channel's own id (Telegram message id). The partial
    unique index on ``(conversation_id, external_id)`` makes a duplicated
    update idempotent: the existing row is returned with ``created=False``.
    """
    if external_id is not None:
        existing = find_message_by_external_id(conn, conversation_id, external_id)
        if existing is not None:
            return existing, False
    try:
        cur = conn.execute(
            """INSERT INTO messages
               (conversation_id, direction, author, type, body, file_path,
                latency_ms, external_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                direction,
                author,
                type,
                body,
                file_path,
                latency_ms,
                external_id,
            ),
        )
    except sqlite3.IntegrityError:
        # A concurrent duplicate won the race: return the stored row.
        conn.rollback()
        existing = find_message_by_external_id(conn, conversation_id, external_id)
        if existing is not None:
            return existing, False
        raise
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conversation_id)
    )
    conn.commit()
    return get_message(conn, cur.lastrowid), True


def find_message_by_external_id(
    conn, conversation_id: int, external_id: str
) -> dict | None:
    row = conn.execute(
        """SELECT * FROM messages
           WHERE conversation_id = ? AND external_id = ?""",
        (conversation_id, external_id),
    ).fetchone()
    return dict(row) if row is not None else None


def get_messages(conn, message_ids: list[int]) -> list[dict]:
    """Fetch messages by id, preserving the order of ``message_ids``."""
    if not message_ids:
        return []
    placeholders = ", ".join("?" for _ in message_ids)
    rows = conn.execute(
        f"SELECT * FROM messages WHERE id IN ({placeholders})",
        tuple(message_ids),
    ).fetchall()
    by_id = {row["id"]: dict(row) for row in rows}
    return [by_id[mid] for mid in message_ids if mid in by_id]


def get_message(conn, message_id: int) -> dict:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise NotFound(f"message {message_id} not found")
    return dict(row)


def recent_messages(conn, conversation_id: int, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM messages WHERE conversation_id = ?
           ORDER BY id DESC LIMIT ?""",
        (conversation_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def message_count(conn, conversation_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()[0]


def messages_before_window(conn, conversation_id: int, window: int = 12) -> list[dict]:
    """Older messages only (excludes the most recent `window`), for the rolling summary."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM messages WHERE conversation_id = ?
               ORDER BY id
               LIMIT (SELECT MAX(0, COUNT(*) - ?) FROM messages WHERE conversation_id = ?)""",
            (conversation_id, window, conversation_id),
        )
    ]


def set_message_body(conn, message_id: int, body: str, *, type: str | None = None) -> None:
    if type is None:
        conn.execute("UPDATE messages SET body = ? WHERE id = ?", (body, message_id))
    else:
        conn.execute(
            "UPDATE messages SET body = ?, type = ? WHERE id = ?", (body, type, message_id)
        )
    conn.commit()


def set_summary(conn, conversation_id: int, summary: str) -> None:
    context = get_context(conn, conversation_id)
    context["summary"] = summary
    context["summary_through"] = message_count(conn, conversation_id)
    set_context(conn, conversation_id, context)
