"""Supplier (TCP) escalations: the critical "ask the provider" loop (Hito 5).

A client asks for a detail the catalog doesn't have -> we open an escalation,
write the question to the TCP over Telegram, and wait. When the TCP answers we
enrich the product's `specifications_json`, resolve the escalation and resume the
client. Status flow:

    PENDING -> ANSWERED -> RESOLVED
    PENDING -> TIMEOUT (after 1 retry) -> stays for the operator
"""

from datetime import datetime, timezone

from cuadra.domain.errors import Invalid, NotFound

OPEN_STATUSES = ("PENDING", "ANSWERED")


def _now() -> str:
    # Match the schema's `datetime('now')` (UTC, no offset) so every timestamp in
    # the DB compares consistently.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get(conn, escalation_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM escalations WHERE id = ?", (escalation_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"escalation {escalation_id} not found")
    return dict(row)


def create(
    conn,
    *,
    conversation_id: int,
    supplier_id: int,
    question: str,
    product_id: int | None = None,
) -> dict:
    question = (question or "").strip()
    if not question:
        raise Invalid("escalation question is required")
    cur = conn.execute(
        """INSERT INTO escalations (conversation_id, product_id, supplier_id, question)
           VALUES (?, ?, ?, ?)""",
        (conversation_id, product_id, supplier_id, question),
    )
    conn.commit()
    return get(conn, cur.lastrowid)


def set_tcp_message_sent(conn, escalation_id: int, text: str) -> dict:
    conn.execute(
        "UPDATE escalations SET tcp_message_sent = ? WHERE id = ?", (text, escalation_id)
    )
    conn.commit()
    return get(conn, escalation_id)


def set_answer(conn, escalation_id: int, answer: str) -> dict:
    """Record the TCP's answer (status ANSWERED)."""
    conn.execute(
        """UPDATE escalations
           SET tcp_answer = ?, status = 'ANSWERED', answered_at = ?
           WHERE id = ?""",
        (answer, _now(), escalation_id),
    )
    conn.commit()
    return get(conn, escalation_id)


def resolve(conn, escalation_id: int) -> dict:
    conn.execute(
        "UPDATE escalations SET status = 'RESOLVED', resolved_at = ? WHERE id = ?",
        (_now(), escalation_id),
    )
    conn.commit()
    return get(conn, escalation_id)


def mark_timeout(conn, escalation_id: int) -> dict:
    conn.execute(
        "UPDATE escalations SET status = 'TIMEOUT', resolved_at = ? WHERE id = ?",
        (_now(), escalation_id),
    )
    conn.commit()
    return get(conn, escalation_id)


def bump_attempts(conn, escalation_id: int) -> dict:
    conn.execute(
        "UPDATE escalations SET attempts = attempts + 1 WHERE id = ?", (escalation_id,)
    )
    conn.commit()
    return get(conn, escalation_id)


def pending_for_conversation(conn, conversation_id: int) -> list[dict]:
    placeholders = ", ".join("?" for _ in OPEN_STATUSES)
    return [
        dict(r)
        for r in conn.execute(
            f"""SELECT * FROM escalations
                WHERE conversation_id = ? AND status IN ({placeholders})
                ORDER BY id""",
            (conversation_id, *OPEN_STATUSES),
        )
    ]


def oldest_pending_for_supplier(conn, supplier_id: int) -> dict | None:
    """The escalation to correlate an incoming TCP answer with (oldest first)."""
    row = conn.execute(
        """SELECT * FROM escalations
           WHERE supplier_id = ? AND status = 'PENDING'
           ORDER BY id LIMIT 1""",
        (supplier_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_escalations(conn, *, status: str | None = None, limit: int = 50) -> list[dict]:
    sql, args = "SELECT * FROM escalations", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def overdue_pending(conn, timeout_min: int) -> list[dict]:
    """PENDING escalations whose question was sent more than `timeout_min` ago.

    Uses the `escalation_sent` audit event timestamp; rows never sent are ignored
    (they are still queued).
    """
    rows = conn.execute(
        """SELECT e.* FROM escalations e
           JOIN events ev
             ON ev.type = 'escalation_sent'
            AND ev.entity = 'escalations' AND ev.entity_id = e.id
           WHERE e.status = 'PENDING'
             AND ev.created_at <= datetime('now', ?)
           ORDER BY e.id""",
        (f"-{int(timeout_min)} minutes",),
    ).fetchall()
    return [dict(r) for r in rows]
