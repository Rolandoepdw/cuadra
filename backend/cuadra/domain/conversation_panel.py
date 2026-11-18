"""Read-only queries for the Conversations panel (phase F1).

The panel answers "what is happening in this chat?" without touching the
pipeline. It joins the identity (`customers`, `customer_channels`), the chat
(`conversations`, `messages`), the turn outcomes written by F0 and the
commercial side (`sales`, `escalations`).

Rules baked in here:

- the list never returns a full phone or address: only masked values;
- `needs_attention` is computed from observable facts, not from an opinion;
- closing is the only write, and it cancels open turns so a stale job cannot
  reply after the operator closed the chat.
"""

import json
from datetime import datetime, timezone

from cuadra.domain import conversations, events
from cuadra.domain.errors import NotFound
from cuadra.domain.pii import mask_address, mask_identifier, mask_phone

QUESTION_INTENTS = (
    "consulta_precio",
    "consulta_stock",
    "consulta_dato",
    "comparar",
)
_QUESTION_INTENTS_SQL = "('consulta_precio','consulta_stock','consulta_dato','comparar')"
_OPEN_ESCALATIONS_SQL = "('PENDING','ANSWERED')"

# The stage lives in the JSON context; old rows may only have it on their last
# turn, so the latest persisted turn is the fallback.
_STAGE_SQL = """COALESCE(
    json_extract(c.context_json, '$.stage'),
    (SELECT stage FROM turns
      WHERE conversation_id = c.id AND stage IS NOT NULL
      ORDER BY id DESC LIMIT 1)
)"""

# An inbound message after the last outbound one means the customer is waiting.
# A closed conversation is deliberately done, so it never asks for attention.
# Supplier (TCP) chats are handled by the escalation flow, not by the operator
# queue, so they never raise this flag.
_NEEDS_ATTENTION_SQL = f"""(
    c.status <> 'CLOSED'
    AND cust.kind = 'customer'
    AND (
        c.status = 'WAITING_TCP'
        OR EXISTS (
            SELECT 1 FROM escalations e
             WHERE e.conversation_id = c.id
               AND e.status IN {_OPEN_ESCALATIONS_SQL}
        )
        OR EXISTS (
            SELECT 1 FROM sales s
             WHERE s.conversation_id = c.id
               AND s.status = 'NEEDS_OPERATOR'
               AND s.id = (SELECT MAX(id) FROM sales WHERE conversation_id = c.id)
        )
        OR (
            SELECT direction FROM messages
             WHERE conversation_id = c.id
             ORDER BY id DESC LIMIT 1
        ) = 'INBOUND'
    )
)"""

_BASE_FROM = "FROM conversations c JOIN customers cust ON cust.id = c.customer_id"

_SELECT = f"""
SELECT
    c.id,
    c.channel,
    c.identity,
    c.status,
    c.created_at,
    c.updated_at,
    c.closed_at,
    {_STAGE_SQL} AS stage,
    cust.id AS customer_id,
    cust.name AS customer_name,
    cust.kind AS customer_kind,
    (
        SELECT identifier FROM customer_channels cc
         WHERE cc.customer_id = cust.id AND cc.channel = c.channel
         ORDER BY cc.id LIMIT 1
    ) AS customer_identifier,
    COALESCE(
        NULLIF(cust.phone, ''),
        NULLIF(json_extract(cust.preferences_json, '$.phone'), ''),
        (
            SELECT s.customer_phone FROM sales s
             WHERE s.conversation_id = c.id AND s.customer_phone IS NOT NULL
             ORDER BY s.id DESC LIMIT 1
        )
    ) AS customer_phone,
    m.body AS last_body,
    m.direction AS last_direction,
    m.author AS last_author,
    m.created_at AS last_message_at,
    (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id) AS messages_count,
    (SELECT COUNT(*) FROM turns
      WHERE conversation_id = c.id AND status = 'DONE') AS turns_count,
    (SELECT COUNT(*) FROM turns
      WHERE conversation_id = c.id AND status = 'DONE'
        AND intent IN {_QUESTION_INTENTS_SQL}) AS questions_count,
    (SELECT COUNT(*) FROM escalations
      WHERE conversation_id = c.id
        AND status IN {_OPEN_ESCALATIONS_SQL}) AS open_escalations,
    ls.id AS sale_id,
    ls.order_code AS sale_order_code,
    ls.status AS sale_status,
    ls.amount AS sale_amount,
    ls.currency AS sale_currency,
    lp.name AS sale_product_name,
    {_NEEDS_ATTENTION_SQL} AS needs_attention
"""

_LIST_FROM = f"""{_BASE_FROM}
LEFT JOIN messages m
       ON m.id = (SELECT MAX(id) FROM messages WHERE conversation_id = c.id)
LEFT JOIN sales ls
       ON ls.id = (SELECT MAX(id) FROM sales WHERE conversation_id = c.id)
LEFT JOIN products lp ON lp.id = ls.product_id
"""

_SORTS = {
    "updated_at": "datetime(c.updated_at) DESC, c.id DESC",
    "created_at": "datetime(c.created_at) DESC, c.id DESC",
    "last_message_at": "datetime(last_message_at) DESC, c.id DESC",
    "messages_count": "messages_count DESC, c.id DESC",
    "turns_count": "turns_count DESC, c.id DESC",
    "questions_count": "questions_count DESC, c.id DESC",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _truncate(value, limit: int = 160) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _like(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _filters(
    *,
    kind: str | None = None,
    status: str | None = None,
    identity: str | None = None,
    channel: str | None = None,
    stage: str | None = None,
    needs_attention: bool | None = None,
    has_escalation: bool | None = None,
    has_sale: bool | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []

    if kind:
        clauses.append("cust.kind = ?")
        args.append(kind)
    if status:
        clauses.append("c.status = ?")
        args.append(status)
    if identity:
        clauses.append("c.identity = ?")
        args.append(identity)
    if channel:
        clauses.append("c.channel = ?")
        args.append(channel)
    if stage:
        clauses.append(f"{_STAGE_SQL} = ?")
        args.append(stage)
    if needs_attention is not None:
        clauses.append(
            _NEEDS_ATTENTION_SQL if needs_attention else f"NOT {_NEEDS_ATTENTION_SQL}"
        )
    if has_escalation is not None:
        exists = (
            "EXISTS (SELECT 1 FROM escalations e WHERE e.conversation_id = c.id"
            f" AND e.status IN {_OPEN_ESCALATIONS_SQL})"
        )
        clauses.append(exists if has_escalation else f"NOT {exists}")
    if has_sale is not None:
        exists = "EXISTS (SELECT 1 FROM sales s WHERE s.conversation_id = c.id)"
        clauses.append(exists if has_sale else f"NOT {exists}")
    if q:
        pattern = _like(q)
        clauses.append(
            """(
                cust.name LIKE ? ESCAPE '\\'
                OR (
                    SELECT identifier FROM customer_channels cc
                     WHERE cc.customer_id = cust.id AND cc.channel = c.channel
                     ORDER BY cc.id LIMIT 1
                ) LIKE ? ESCAPE '\\'
                OR json_extract(cust.preferences_json, '$.phone') LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM sales s
                     WHERE s.conversation_id = c.id
                       AND s.order_code LIKE ? ESCAPE '\\'
                )
                OR EXISTS (
                    SELECT 1 FROM messages m2
                     WHERE m2.conversation_id = c.id
                       AND m2.body LIKE ? ESCAPE '\\'
                )
            )"""
        )
        args.extend([pattern] * 5)
    if date_from:
        clauses.append("datetime(c.updated_at) >= datetime(?)")
        args.append(date_from)
    if date_to:
        clauses.append("datetime(c.updated_at) < datetime(?, '+1 day')")
        args.append(date_to)

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


def _item(row) -> dict:
    last_message = None
    if row["last_message_at"] is not None:
        last_message = {
            "body": _truncate(row["last_body"]),
            "direction": row["last_direction"],
            "author": row["last_author"],
            "created_at": row["last_message_at"],
        }
    last_sale = None
    if row["sale_id"] is not None:
        last_sale = {
            "id": row["sale_id"],
            "order_code": row["sale_order_code"],
            "status": row["sale_status"],
            "amount": row["sale_amount"],
            "currency": row["sale_currency"],
            "product_name": row["sale_product_name"],
        }
    return {
        "id": row["id"],
        "kind": row["customer_kind"],
        "channel": row["channel"],
        "identity": row["identity"],
        "status": row["status"],
        "stage": row["stage"],
        "customer": {
            "id": row["customer_id"],
            "name": row["customer_name"],
            "handle": mask_identifier(row["customer_identifier"]),
            "phone_masked": mask_phone(row["customer_phone"]),
        },
        "last_message": last_message,
        "messages_count": row["messages_count"],
        "turns_count": row["turns_count"],
        "questions_count": row["questions_count"],
        "open_escalations": row["open_escalations"],
        "last_sale": last_sale,
        "needs_attention": bool(row["needs_attention"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
    }


def list_conversations(
    conn,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = "updated_at",
    **filters,
) -> dict:
    where, args = _filters(**filters)
    order = _SORTS.get(sort or "updated_at", _SORTS["updated_at"])
    rows = conn.execute(
        f"{_SELECT} {_LIST_FROM} {where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) {_BASE_FROM} {where}", tuple(args)
    ).fetchone()[0]
    return {
        "items": [_item(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _get_item(conn, conversation_id: int) -> dict:
    row = conn.execute(
        f"{_SELECT} {_LIST_FROM} WHERE c.id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"conversation {conversation_id} not found")
    return _item(row)


def summary(conn) -> dict:
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN c.status IN ('ACTIVE','WAITING_TCP') THEN 1 ELSE 0 END) AS open,
            SUM(CASE WHEN c.status = 'WAITING_TCP' THEN 1 ELSE 0 END) AS waiting_tcp,
            SUM(CASE WHEN {_NEEDS_ATTENTION_SQL} THEN 1 ELSE 0 END) AS needs_attention,
            SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM escalations e
                     WHERE e.conversation_id = c.id
                       AND e.status IN {_OPEN_ESCALATIONS_SQL}
                ) THEN 1 ELSE 0 END) AS with_open_escalation,
            SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM sales s WHERE s.conversation_id = c.id
                ) THEN 1 ELSE 0 END) AS with_sale,
            SUM(CASE WHEN cust.kind = 'customer' THEN 1 ELSE 0 END) AS customers,
            SUM(CASE WHEN cust.kind = 'supplier' THEN 1 ELSE 0 END) AS suppliers
        {_BASE_FROM}
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


# ------------------------------------------------------------------- detail

_PHONE_KEYS = {
    "phone",
    "customer_phone",
    "telefono",
    "teléfono",
    "celular",
    "whatsapp",
}
_ADDRESS_KEYS = {
    "address",
    "delivery_address",
    "direccion",
    "dirección",
    "usual_address",
}


def _sanitize_context(value, key: str | None = None):
    """Drop control keys and mask phone/address values, recursively."""
    if isinstance(value, dict):
        return {
            item_key: _sanitize_context(item_value, item_key)
            for item_key, item_value in value.items()
            if not str(item_key).startswith("_")
        }
    if isinstance(value, list):
        return [_sanitize_context(item) for item in value]
    if key is not None:
        normalized = key.casefold()
        if normalized in _PHONE_KEYS:
            return mask_phone(value)
        if normalized in _ADDRESS_KEYS:
            return mask_address(value)
    return value


def _facts(conn, conversation_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT fact_key, fact_value, confidence, updated_at
             FROM conversation_facts
            WHERE conversation_id = ?
            ORDER BY fact_key""",
        (conversation_id,),
    ).fetchall()
    facts = []
    for row in rows:
        value = row["fact_value"]
        key = row["fact_key"]
        if key in _PHONE_KEYS:
            value = mask_phone(value)
        elif key in _ADDRESS_KEYS:
            value = mask_address(value)
        facts.append(
            {
                "fact_key": key,
                "fact_value": value,
                "confidence": row["confidence"],
                "updated_at": row["updated_at"],
            }
        )
    return facts


def _messages(
    conn, conversation_id: int, *, limit: int, before_id: int | None = None
) -> tuple[list[dict], bool]:
    if before_id is not None:
        rows = conn.execute(
            """SELECT * FROM messages
                WHERE conversation_id = ? AND id < ?
                ORDER BY id DESC LIMIT ?""",
            (conversation_id, before_id, limit + 1),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC LIMIT ?""",
            (conversation_id, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": row["id"],
            "direction": row["direction"],
            "author": row["author"],
            "type": row["type"],
            "body": row["body"],
            "file_path": row["file_path"],
            "latency_ms": row["latency_ms"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]
    return items, has_more


def messages_page(
    conn,
    conversation_id: int,
    *,
    limit: int = 50,
    before_id: int | None = None,
) -> dict:
    conversations.get_conversation(conn, conversation_id)
    items, has_more = _messages(
        conn, conversation_id, limit=limit, before_id=before_id
    )
    return {"items": items, "has_more": has_more, "limit": limit}


def _sales(conn, conversation_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.order_code, s.status, s.quantity, s.amount,
                  s.currency, s.product_id, p.name AS product_name,
                  s.created_at, s.updated_at
             FROM sales s
             LEFT JOIN products p ON p.id = s.product_id
            WHERE s.conversation_id = ?
            ORDER BY s.id DESC""",
        (conversation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _escalations(conn, conversation_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT e.id, e.product_id, p.name AS product_name,
                  e.supplier_id, sup.name AS supplier_name,
                  e.question, e.tcp_answer, e.status,
                  e.created_at, e.answered_at, e.resolved_at
             FROM escalations e
             LEFT JOIN products p ON p.id = e.product_id
             LEFT JOIN suppliers sup ON sup.id = e.supplier_id
            WHERE e.conversation_id = ?
            ORDER BY e.id DESC""",
        (conversation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _turns(conn, conversation_id: int, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT t.id, t.status, t.intent, t.product_id,
                  p.name AS product_name, t.stage, t.generator,
                  t.escalated, t.sale_id, t.latency_ms, t.outcome,
                  t.created_at, t.finished_at
             FROM turns t
             LEFT JOIN products p ON p.id = t.product_id
            WHERE t.conversation_id = ?
            ORDER BY t.id DESC LIMIT ?""",
        (conversation_id, limit),
    ).fetchall()
    return [
        {
            **dict(row),
            "escalated": bool(row["escalated"]),
        }
        for row in rows
    ]


def _traces(conn, conversation_id: int, *, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT id, method, ok, duration_ms, timestamp
             FROM traces
            WHERE conversation_id = ?
            ORDER BY id DESC LIMIT ?""",
        (conversation_id, limit),
    ).fetchall()
    return [{**dict(row), "ok": bool(row["ok"])} for row in rows]


def _customer_detail(conn, conversation: dict) -> dict:
    customer = conversations.get_customer(conn, conversation["customer"]["id"])
    prefs = _json(customer.get("preferences_json"))
    zone_name = prefs.get("usual_zone")
    if customer.get("usual_zone_id"):
        zone = conn.execute(
            "SELECT name FROM zones WHERE id = ?", (customer["usual_zone_id"],)
        ).fetchone()
        if zone is not None:
            zone_name = zone["name"]
    address = customer.get("usual_address") or prefs.get("usual_address")
    phone = customer.get("phone") or prefs.get("phone")
    return {
        "id": customer["id"],
        "name": customer.get("name"),
        "display_name": customer.get("display_name"),
        "kind": customer.get("kind", "customer"),
        "handle": conversation["customer"]["handle"],
        "phone_masked": mask_phone(phone)
        or conversation["customer"]["phone_masked"],
        "usual_zone": zone_name,
        "usual_address_masked": mask_address(address),
        "notes": prefs.get("notes"),
        "created_at": customer.get("created_at"),
    }


def get_detail(
    conn, conversation_id: int, *, message_limit: int = 100
) -> dict:
    conversation = _get_item(conn, conversation_id)
    messages, has_more = _messages(
        conn, conversation_id, limit=message_limit
    )
    return {
        "conversation": conversation,
        "customer": _customer_detail(conn, conversation),
        "context": _sanitize_context(
            conversations.get_context(conn, conversation_id)
        ),
        "facts": _facts(conn, conversation_id),
        "messages": messages,
        "messages_has_more": has_more,
        "sales": _sales(conn, conversation_id),
        "escalations": _escalations(conn, conversation_id),
        "turns": _turns(conn, conversation_id),
        "traces": _traces(conn, conversation_id),
    }


def close(conn, conversation_id: int) -> dict:
    """Close a conversation and cancel the turns that have not run yet.

    A turn already PROCESSING is allowed to finish: its reply is the answer to
    a message the customer already sent. Only OPEN turns (and their pending
    jobs) are cancelled, so a stale job cannot answer after the operator closed
    the chat.
    """
    conversation = conversations.get_conversation(conn, conversation_id)
    if conversation["status"] != "CLOSED":
        now = _now()
        conn.execute(
            """UPDATE conversations
                  SET status = 'CLOSED', closed_at = ?, updated_at = ?
                WHERE id = ?""",
            (now, now, conversation_id),
        )
        conn.execute(
            """UPDATE turns SET status = 'CANCELLED', updated_at = ?
                WHERE conversation_id = ? AND status = 'OPEN'""",
            (now, conversation_id),
        )
        conn.execute(
            """DELETE FROM jobs
                WHERE type = 'process_turn' AND status = 'PENDING'
                  AND json_extract(payload_json, '$.turn_id') IN (
                      SELECT id FROM turns WHERE conversation_id = ?
                  )""",
            (conversation_id,),
        )
        conn.commit()
        events.log(
            conn,
            "dashboard",
            "conversation_closed",
            category="conversations",
            entity="conversations",
            entity_id=conversation_id,
            details={"previous_status": conversation["status"]},
        )
    return _get_item(conn, conversation_id)
