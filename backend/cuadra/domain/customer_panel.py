"""Customer panel queries and administrative actions (phase F3).

The panel answers two different questions without mixing them:

- **effort**: how much attention a customer consumed (turns, messages,
  escalations, compute time);
- **outcome**: what that effort produced (settled sales and collected
  commission).

Money is never invented and USD is never added to CUP. Segments come from those
two axes; a confirmed risk signal can only move a customer to `riesgo`, never
block it. Blocking, merging and revealing PII are explicit operator actions,
always audited in `events`.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from cuadra.domain import catalog, conversations, customers, events, settings
from cuadra.domain.errors import Invalid, NotFound
from cuadra.domain.pii import mask_address, mask_identifier, mask_phone

QUESTION_INTENTS = (
    "consulta_precio",
    "consulta_stock",
    "consulta_dato",
    "comparar",
)
_QUESTION_INTENTS_SQL = "('consulta_precio','consulta_stock','consulta_dato','comparar')"
_OPEN_ESCALATIONS_SQL = "('PENDING','ANSWERED')"
_ACTIVE_CONVERSATIONS_SQL = "('ACTIVE','WAITING_TCP')"

SEGMENTS = (
    "bloqueado",
    "riesgo",
    "vip",
    "comprador",
    "potencial",
    "poco_rentable",
    "frio",
    "nuevo",
)

RISK_WEIGHTS = {
    "abuso_verbal": 40,
    "pedido_falso": 40,
    "flood": 25,
    "scraping_catalogo": 20,
    "cancelaciones_repetidas": 20,
    "reclamos_repetidos": 15,
    "muchas_preguntas_sin_compra": 10,
    "posible_duplicado": 5,
}

# Conservative: only clear insults/threats, and only as a signal to review.
_ABUSE_RE = re.compile(
    r"\b(?:hij[oa]p+uta|malparid[oa]|imb[eé]cil|est[uú]pid[oa]|"
    r"idiota|basura|ladr[oó]n|te voy a (?:matar|joder|partir)|"
    r"me cago en|hijo de puta|come mierda|vete al carajo)\b",
    re.IGNORECASE,
)

_DEFAULT_THRESHOLDS = {
    "customers.segment.vip_min_settled": 3,
    "customers.segment.vip_days": 90,
    "customers.segment.potential_days": 30,
    "customers.segment.cold_days": 90,
    "customers.segment.low_value_min_turns": 20,
    "customers.segment.low_value_min_conversations": 3,
    "customers.risk.medium": 30,
    "customers.risk.high": 60,
}

_METRICS_SELECT = f"""
SELECT
    c.id,
    c.name,
    c.display_name,
    c.kind,
    c.status,
    COALESCE(
        NULLIF(c.phone, ''),
        NULLIF(c.phone_normalized, ''),
        (SELECT cc.normalized FROM customer_contacts cc
          WHERE cc.customer_id = c.id AND cc.kind = 'phone'
          ORDER BY cc.verified DESC, cc.id LIMIT 1)
    ) AS phone,
    c.phone_normalized,
    c.usual_zone_id,
    c.usual_address,
    c.merged_into_id,
    c.created_at,
    COALESCE(
        c.first_seen_at,
        (SELECT MIN(cv.created_at) FROM conversations cv WHERE cv.customer_id = c.id)
    ) AS first_seen_at,
    COALESCE(
        c.last_seen_at,
        (SELECT MAX(cv.updated_at) FROM conversations cv WHERE cv.customer_id = c.id)
    ) AS last_seen_at,
    z.name AS zone_name,
    (SELECT COUNT(*) FROM conversations cv WHERE cv.customer_id = c.id) AS conversations_total,
    (SELECT COUNT(*) FROM conversations cv
      WHERE cv.customer_id = c.id AND cv.status IN {_ACTIVE_CONVERSATIONS_SQL}) AS conversations_open,
    (SELECT COUNT(*) FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
      WHERE cv.customer_id = c.id AND m.direction = 'INBOUND') AS messages_in,
    (SELECT COUNT(*) FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
      WHERE cv.customer_id = c.id AND m.direction = 'OUTBOUND') AS messages_out,
    (SELECT COUNT(*) FROM turns t JOIN conversations cv ON cv.id = t.conversation_id
      WHERE cv.customer_id = c.id AND t.outcome = 'ok') AS turns_total,
    (SELECT COUNT(*) FROM turns t JOIN conversations cv ON cv.id = t.conversation_id
      WHERE cv.customer_id = c.id AND t.outcome = 'ok'
        AND t.intent IN {_QUESTION_INTENTS_SQL}) AS questions_total,
    (SELECT COUNT(DISTINCT t.product_id) FROM turns t JOIN conversations cv ON cv.id = t.conversation_id
      WHERE cv.customer_id = c.id AND t.product_id IS NOT NULL) AS products_inquired,
    (SELECT COUNT(*) FROM escalations e JOIN conversations cv ON cv.id = e.conversation_id
      WHERE cv.customer_id = c.id) AS escalations_total,
    (SELECT COUNT(*) FROM escalations e JOIN conversations cv ON cv.id = e.conversation_id
      WHERE cv.customer_id = c.id AND e.status IN {_OPEN_ESCALATIONS_SQL}) AS escalations_open,
    (SELECT COUNT(*) FROM sales s WHERE s.customer_id = c.id AND s.status <> 'CANCELLED') AS sales_created,
    (SELECT COUNT(*) FROM sales s WHERE s.customer_id = c.id AND s.status = 'SETTLED') AS sales_settled,
    (SELECT COUNT(*) FROM sales s WHERE s.customer_id = c.id AND s.status = 'CANCELLED') AS sales_cancelled,
    (SELECT COUNT(*) FROM sales s WHERE s.customer_id = c.id AND s.status = 'NEEDS_OPERATOR') AS sales_needs_operator,
    (SELECT COALESCE(SUM(s.amount), 0) FROM sales s
      WHERE s.customer_id = c.id AND s.currency = 'USD' AND s.status <> 'CANCELLED') AS revenue_usd,
    (SELECT COALESCE(SUM(s.amount), 0) FROM sales s
      WHERE s.customer_id = c.id AND s.currency = 'CUP' AND s.status <> 'CANCELLED') AS revenue_cup,
    (SELECT COALESCE(SUM(cl.commission_earned), 0) FROM commission_ledger cl
      JOIN sales s ON s.id = cl.sale_id
      WHERE s.customer_id = c.id AND cl.commission_currency = 'USD'
        AND cl.status = 'COLLECTED') AS commission_usd,
    (SELECT COALESCE(SUM(cl.commission_earned), 0) FROM commission_ledger cl
      JOIN sales s ON s.id = cl.sale_id
      WHERE s.customer_id = c.id AND cl.commission_currency = 'CUP'
        AND cl.status = 'COLLECTED') AS commission_cup,
    (SELECT COALESCE(SUM((COALESCE(t.intent_ms, 0) + COALESCE(t.compose_ms, 0)) / 1000.0), 0)
       FROM turns t JOIN conversations cv ON cv.id = t.conversation_id
      WHERE cv.customer_id = c.id) AS compute_s,
    (SELECT MAX(s.created_at) FROM sales s
      WHERE s.customer_id = c.id AND s.status <> 'CANCELLED') AS last_purchase_at
FROM customers c
LEFT JOIN zones z ON z.id = c.usual_zone_id
"""

_SORTS = {
    "last_seen_at": "last_seen_at",
    "first_seen_at": "first_seen_at",
    "name": "name",
    "created_at": "created_at",
    "conversations_total": "conversations_total",
    "turns_total": "turns_total",
    "questions_total": "questions_total",
    "sales_settled": "sales_settled",
    "revenue_usd": "revenue_usd",
    "effort_score": "effort_score",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _like(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _clean(value, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text[:19])
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _thresholds(conn) -> dict:
    values = {}
    for key, default in _DEFAULT_THRESHOLDS.items():
        raw = settings.get(conn, key, str(default))
        try:
            values[key] = int(raw)
        except (TypeError, ValueError):
            values[key] = default
    return values


# ------------------------------------------------------------------- metrics


def _metrics(row) -> dict:
    data = dict(row)
    conversations_total = int(data.get("conversations_total") or 0)
    turns_total = int(data.get("turns_total") or 0)
    messages_in = int(data.get("messages_in") or 0)
    escalations_total = int(data.get("escalations_total") or 0)
    sales_settled = int(data.get("sales_settled") or 0)
    data.update(
        {
            "conversations_total": conversations_total,
            "conversations_open": int(data.get("conversations_open") or 0),
            "messages_in": messages_in,
            "messages_out": int(data.get("messages_out") or 0),
            "turns_total": turns_total,
            "questions_total": int(data.get("questions_total") or 0),
            "products_inquired": int(data.get("products_inquired") or 0),
            "escalations_total": escalations_total,
            "escalations_open": int(data.get("escalations_open") or 0),
            "sales_created": int(data.get("sales_created") or 0),
            "sales_settled": sales_settled,
            "sales_cancelled": int(data.get("sales_cancelled") or 0),
            "sales_needs_operator": int(data.get("sales_needs_operator") or 0),
            "revenue_usd": float(data.get("revenue_usd") or 0.0),
            "revenue_cup": float(data.get("revenue_cup") or 0.0),
            "commission_usd": float(data.get("commission_usd") or 0.0),
            "commission_cup": float(data.get("commission_cup") or 0.0),
            "compute_s": round(float(data.get("compute_s") or 0.0), 2),
            "conversion": (
                round(sales_settled / conversations_total, 4)
                if conversations_total
                else 0.0
            ),
            "avg_turns_per_conversation": (
                round(turns_total / conversations_total, 2)
                if conversations_total
                else 0.0
            ),
            "effort_score": round(
                turns_total + 2 * escalations_total + messages_in / 2, 1
            ),
        }
    )
    return data


def _segment(metrics: dict, risk_score: int, thresholds: dict) -> str:
    status = metrics.get("status") or "ACTIVE"
    now = datetime.now(timezone.utc)
    last = _parse_dt(metrics.get("last_seen_at"))
    first = _parse_dt(metrics.get("first_seen_at"))
    last_purchase = _parse_dt(metrics.get("last_purchase_at"))
    days_since_last = (now - last).days if last else None
    days_since_first = (now - first).days if first else None
    days_since_purchase = (now - last_purchase).days if last_purchase else None

    if status in ("BLOCKED", "SPAM"):
        return "bloqueado"
    if risk_score >= thresholds["customers.risk.high"]:
        return "riesgo"
    if (
        metrics["sales_settled"] >= thresholds["customers.segment.vip_min_settled"]
        and days_since_purchase is not None
        and days_since_purchase <= thresholds["customers.segment.vip_days"]
    ):
        return "vip"
    if metrics["sales_settled"] >= 1:
        return "comprador"
    # High effort with zero outcome wins over "potential": 25 turns and no sale
    # is the case the operator needs to see (plan §5.9), not a warm lead.
    if metrics["sales_settled"] == 0 and (
        metrics["turns_total"] >= thresholds["customers.segment.low_value_min_turns"]
        or metrics["conversations_total"]
        >= thresholds["customers.segment.low_value_min_conversations"]
    ):
        if (
            days_since_last is not None
            and days_since_last <= thresholds["customers.segment.cold_days"]
        ):
            return "poco_rentable"
    if (
        metrics["sales_settled"] == 0
        and metrics["questions_total"] >= 1
        and days_since_last is not None
        and days_since_last <= thresholds["customers.segment.potential_days"]
    ):
        return "potencial"
    if (
        metrics["sales_settled"] == 0
        and days_since_last is not None
        and days_since_last > thresholds["customers.segment.cold_days"]
    ):
        return "frio"
    if days_since_first is not None and days_since_first <= 7:
        return "nuevo"
    if days_since_last is None:
        return "frio"
    return "potencial" if metrics["questions_total"] else "nuevo"


def _risk_level(score: int, thresholds: dict) -> str:
    if score >= thresholds["customers.risk.high"]:
        return "high"
    if score >= thresholds["customers.risk.medium"]:
        return "medium"
    return "low"


def _confirmed_kinds(conn, customer_ids: list[int]) -> dict[int, list[str]]:
    if not customer_ids:
        return {}
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""SELECT customer_id, kind FROM customer_signals
             WHERE status = 'CONFIRMED' AND customer_id IN ({placeholders})""",
        tuple(customer_ids),
    ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(int(row["customer_id"]), []).append(row["kind"])
    return result


def _risk_scores(conn, customer_ids: list[int]) -> dict[int, int]:
    kinds = _confirmed_kinds(conn, customer_ids)
    return {
        customer_id: sum(RISK_WEIGHTS.get(kind, 0) for kind in values)
        for customer_id, values in kinds.items()
    }


def _channels(conn, customer_ids: list[int]) -> dict[int, list[dict]]:
    if not customer_ids:
        return {}
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""SELECT customer_id, channel, identifier FROM customer_channels
             WHERE customer_id IN ({placeholders}) ORDER BY id""",
        tuple(customer_ids),
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for row in rows:
        result.setdefault(int(row["customer_id"]), []).append(
            {
                "channel": row["channel"],
                "handle": mask_identifier(row["identifier"]),
            }
        )
    return result


def _tags(conn, customer_ids: list[int]) -> dict[int, list[str]]:
    if not customer_ids:
        return {}
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""SELECT customer_id, tag FROM customer_tags
             WHERE customer_id IN ({placeholders}) ORDER BY tag""",
        tuple(customer_ids),
    ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(int(row["customer_id"]), []).append(row["tag"])
    return result


def _list_item(
    metrics: dict,
    *,
    risk_score: int,
    segment: str,
    risk: str,
    channels: list[dict],
    tags: list[str],
) -> dict:
    return {
        "id": metrics["id"],
        "name": metrics.get("name"),
        "display_name": metrics.get("display_name"),
        "kind": metrics.get("kind", "customer"),
        "status": metrics.get("status") or "ACTIVE",
        "phone_masked": mask_phone(metrics.get("phone") or metrics.get("phone_normalized")),
        "zone": metrics.get("zone_name"),
        "channels": channels,
        "conversations_total": metrics["conversations_total"],
        "conversations_open": metrics["conversations_open"],
        "questions_total": metrics["questions_total"],
        "turns_total": metrics["turns_total"],
        "sales_created": metrics["sales_created"],
        "sales_settled": metrics["sales_settled"],
        "revenue_usd": metrics["revenue_usd"],
        "revenue_cup": metrics["revenue_cup"],
        "commission_usd": metrics["commission_usd"],
        "commission_cup": metrics["commission_cup"],
        "conversion": metrics["conversion"],
        "effort_score": metrics["effort_score"],
        "first_seen_at": metrics.get("first_seen_at"),
        "last_seen_at": metrics.get("last_seen_at"),
        "last_purchase_at": metrics.get("last_purchase_at"),
        "segment": segment,
        "risk": risk,
        "risk_score": risk_score,
        "tags": tags,
    }


def _where(
    *,
    kind: str | None = "customer",
    status: str | None = None,
    zone_id: int | None = None,
    tag: str | None = None,
    has_sales: bool | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []
    if kind:
        clauses.append("c.kind = ?")
        args.append(kind)
    if status:
        clauses.append("c.status = ?")
        args.append(status)
    if zone_id:
        clauses.append("c.usual_zone_id = ?")
        args.append(zone_id)
    if tag:
        clauses.append(
            "EXISTS (SELECT 1 FROM customer_tags ct WHERE ct.customer_id = c.id AND ct.tag = ?)"
        )
        args.append(tag)
    if has_sales is not None:
        exists = "EXISTS (SELECT 1 FROM sales s WHERE s.customer_id = c.id)"
        clauses.append(exists if has_sales else f"NOT {exists}")
    if q:
        pattern = _like(q)
        clauses.append(
            """(
                c.name LIKE ? ESCAPE '\\'
                OR c.display_name LIKE ? ESCAPE '\\'
                OR c.phone_normalized LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM customer_contacts cc
                     WHERE cc.customer_id = c.id AND cc.normalized LIKE ? ESCAPE '\\'
                )
                OR EXISTS (
                    SELECT 1 FROM customer_channels ch
                     WHERE ch.customer_id = c.id AND ch.identifier LIKE ? ESCAPE '\\'
                )
                OR EXISTS (
                    SELECT 1 FROM sales s
                     WHERE s.customer_id = c.id AND s.order_code LIKE ? ESCAPE '\\'
                )
            )"""
        )
        args.extend([pattern] * 6)
    if date_from:
        clauses.append(
            "datetime(COALESCE(c.last_seen_at, c.created_at)) >= datetime(?)"
        )
        args.append(date_from)
    if date_to:
        clauses.append(
            "datetime(COALESCE(c.last_seen_at, c.created_at)) < datetime(?, '+1 day')"
        )
        args.append(date_to)
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


def list_customers(
    conn,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = "last_seen_at",
    segment: str | None = None,
    risk: str | None = None,
    **filters,
) -> dict:
    where, args = _where(**filters)
    rows = conn.execute(
        f"{_METRICS_SELECT} {where} ORDER BY c.id", tuple(args)
    ).fetchall()
    metrics = [_metrics(row) for row in rows]
    ids = [int(item["id"]) for item in metrics]
    scores = _risk_scores(conn, ids)
    thresholds = _thresholds(conn)

    items: list[dict] = []
    for item in metrics:
        score = scores.get(int(item["id"]), 0)
        item_segment = _segment(item, score, thresholds)
        item_risk = _risk_level(score, thresholds)
        if segment and item_segment != segment:
            continue
        if risk and item_risk != risk:
            continue
        items.append(
            {
                **item,
                "segment": item_segment,
                "risk": item_risk,
                "risk_score": score,
            }
        )

    reverse = True
    order_key = _SORTS.get(sort or "last_seen_at", "last_seen_at")

    def sort_value(item: dict):
        value = item.get(order_key)
        if isinstance(value, str):
            parsed = _parse_dt(value)
            return parsed.timestamp() if parsed else 0.0
        if value is None:
            return 0
        return value

    items.sort(key=sort_value, reverse=reverse)
    total = len(items)
    page = items[offset : offset + limit]
    page_ids = [int(item["id"]) for item in page]
    channels = _channels(conn, page_ids)
    tags = _tags(conn, page_ids)

    return {
        "items": [
            _list_item(
                item,
                risk_score=int(item["risk_score"]),
                segment=item["segment"],
                risk=item["risk"],
                channels=channels.get(int(item["id"]), []),
                tags=tags.get(int(item["id"]), []),
            )
            for item in page
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "segments": list(SEGMENTS),
    }


def summary(conn) -> dict:
    rows = conn.execute(
        f"{_METRICS_SELECT} WHERE c.kind = 'customer'"
    ).fetchall()
    metrics = [_metrics(row) for row in rows]
    ids = [int(item["id"]) for item in metrics]
    scores = _risk_scores(conn, ids)
    thresholds = _thresholds(conn)
    now = datetime.now(timezone.utc)

    total = len(metrics)
    buyers = 0
    new_7d = 0
    active_7d = 0
    risk_count = 0
    blocked = 0
    low_value = 0
    revenue_usd = 0.0
    revenue_cup = 0.0
    commission_usd = 0.0
    commission_cup = 0.0

    for item in metrics:
        customer_id = int(item["id"])
        score = scores.get(customer_id, 0)
        item_segment = _segment(item, score, thresholds)
        first = _parse_dt(item.get("first_seen_at"))
        last = _parse_dt(item.get("last_seen_at"))
        if item["sales_settled"] >= 1:
            buyers += 1
        if first and (now - first).days <= 7:
            new_7d += 1
        if last and (now - last).days <= 7:
            active_7d += 1
        if item_segment == "riesgo":
            risk_count += 1
        if item_segment == "bloqueado":
            blocked += 1
        if item_segment == "poco_rentable":
            low_value += 1
        revenue_usd += item["revenue_usd"]
        revenue_cup += item["revenue_cup"]
        commission_usd += item["commission_usd"]
        commission_cup += item["commission_cup"]

    return {
        "total": total,
        "new_7d": new_7d,
        "active_7d": active_7d,
        "buyers": buyers,
        "conversion": round(buyers / total, 4) if total else 0.0,
        "risk": risk_count,
        "blocked": blocked,
        "low_value": low_value,
        "revenue_usd": round(revenue_usd, 2),
        "revenue_cup": round(revenue_cup, 2),
        "commission_usd": round(commission_usd, 2),
        "commission_cup": round(commission_cup, 2),
    }


# -------------------------------------------------------------------- detail


def _contacts(conn, customer_id: int) -> list[dict]:
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "value_masked": customers.mask_contact(row["kind"], row["value"]),
            "normalized_masked": customers.mask_contact(
                row["kind"], row["normalized"]
            ),
            "label": row["label"],
            "source": row["source"],
            "confidence": row["confidence"],
            "verified": bool(row["verified"]),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
        }
        for row in customers.contacts_for(conn, customer_id)
    ]


def _zones_used(conn, customer_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT DISTINCT z.id, z.name
             FROM sales s JOIN zones z ON z.id = s.delivery_zone_id
            WHERE s.customer_id = ? AND z.id IS NOT NULL
            ORDER BY z.name""",
        (customer_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _notes(conn, customer_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, body, author, created_at FROM customer_notes
            WHERE customer_id = ? ORDER BY id DESC""",
        (customer_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _signals(conn, customer_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM customer_signals WHERE customer_id = ?
            ORDER BY (status = 'OPEN') DESC, severity DESC, id DESC""",
        (customer_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["evidence"] = _json(item.pop("evidence_json", None))
        items.append(item)
    return items


def _conversations(conn, customer_id: int, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT c.id, c.channel, c.identity, c.status, c.created_at,
                  c.updated_at, c.closed_at,
                  (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS messages_count,
                  (SELECT m.created_at FROM messages m WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS last_message_at
             FROM conversations c
            WHERE c.customer_id = ?
            ORDER BY c.id DESC LIMIT ?""",
        (customer_id, limit),
    ).fetchall()
    return [
        {
            **dict(row),
            "handle": mask_identifier(row["identity"]),
        }
        for row in rows
    ]


def _sales(conn, customer_id: int, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.order_code, s.status, s.quantity, s.amount,
                  s.currency, s.product_id, p.name AS product_name,
                  s.created_at, s.updated_at
             FROM sales s LEFT JOIN products p ON p.id = s.product_id
            WHERE s.customer_id = ?
            ORDER BY s.id DESC LIMIT ?""",
        (customer_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _get_metrics(conn, customer_id: int) -> dict:
    row = conn.execute(
        f"{_METRICS_SELECT} WHERE c.id = ?", (customer_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"customer {customer_id} not found")
    return _metrics(row)


def get_detail(conn, customer_id: int) -> dict:
    metrics = _get_metrics(conn, customer_id)
    score = _risk_scores(conn, [customer_id]).get(customer_id, 0)
    thresholds = _thresholds(conn)
    item_segment = _segment(metrics, score, thresholds)
    tags = _tags(conn, [customer_id]).get(customer_id, [])
    channels = _channels(conn, [customer_id]).get(customer_id, [])
    return {
        "customer": {
            **_list_item(
                metrics,
                risk_score=score,
                segment=item_segment,
                risk=_risk_level(score, thresholds),
                channels=channels,
                tags=tags,
            ),
            "phone_masked": mask_phone(
                metrics.get("phone") or metrics.get("phone_normalized")
            ),
            "address_masked": mask_address(metrics.get("usual_address")),
            "created_at": metrics.get("created_at"),
        },
        "metrics": {
            "conversations_total": metrics["conversations_total"],
            "conversations_open": metrics["conversations_open"],
            "messages_in": metrics["messages_in"],
            "messages_out": metrics["messages_out"],
            "turns_total": metrics["turns_total"],
            "questions_total": metrics["questions_total"],
            "products_inquired": metrics["products_inquired"],
            "escalations_total": metrics["escalations_total"],
            "escalations_open": metrics["escalations_open"],
            "sales_created": metrics["sales_created"],
            "sales_settled": metrics["sales_settled"],
            "sales_cancelled": metrics["sales_cancelled"],
            "sales_needs_operator": metrics["sales_needs_operator"],
            "revenue_usd": metrics["revenue_usd"],
            "revenue_cup": metrics["revenue_cup"],
            "commission_usd": metrics["commission_usd"],
            "commission_cup": metrics["commission_cup"],
            "conversion": metrics["conversion"],
            "avg_turns_per_conversation": metrics["avg_turns_per_conversation"],
            "compute_s": metrics["compute_s"],
            "effort_score": metrics["effort_score"],
            "first_seen_at": metrics.get("first_seen_at"),
            "last_seen_at": metrics.get("last_seen_at"),
            "last_purchase_at": metrics.get("last_purchase_at"),
            "risk_score": score,
        },
        "contacts": _contacts(conn, customer_id),
        "zones": _zones_used(conn, customer_id),
        "tags": tags,
        "notes": _notes(conn, customer_id),
        "signals": _signals(conn, customer_id),
        "conversations": _conversations(conn, customer_id),
        "sales": _sales(conn, customer_id),
        "merge": {
            "merged_into_id": metrics.get("merged_into_id"),
        },
    }


def reveal_contact(conn, customer_id: int) -> dict:
    customer = conversations.get_customer(conn, customer_id)
    contacts = customers.contacts_for(conn, customer_id)
    phone = customer.get("phone") or customer.get("phone_normalized")
    if not phone:
        phone = next(
            (
                row["normalized"]
                for row in contacts
                if row["kind"] in ("phone", "whatsapp")
            ),
            None,
        )
    payload = {
        "customer_id": customer_id,
        "phone": phone,
        "address": customer.get("usual_address"),
        "contacts": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "value": row["value"],
                "normalized": row["normalized"],
                "source": row["source"],
                "verified": bool(row["verified"]),
            }
            for row in contacts
        ],
    }
    events.log(
        conn,
        "dashboard",
        "customer_pii_revealed",
        entity="customers",
        entity_id=customer_id,
        details={"fields": ["phone", "address", "contacts"]},
    )
    conn.commit()
    return payload


# ------------------------------------------------------------------- actions


def _log(conn, event_type: str, customer_id: int, details: dict | None = None) -> None:
    events.log(
        conn,
        "dashboard",
        event_type,
        entity="customers",
        entity_id=customer_id,
        details=details,
    )
    conn.commit()


def update_profile(conn, customer_id: int, data: dict) -> dict:
    conversations.get_customer(conn, customer_id)
    changes: dict = {}

    name = data.get("name")
    if name is not None:
        changes["name"] = _clean(name, 80)
    display_name = data.get("display_name")
    if display_name is not None:
        changes["display_name"] = _clean(display_name, 120)
    status = data.get("status")
    if status is not None:
        if status not in ("ACTIVE", "BLOCKED", "SPAM"):
            raise Invalid("invalid customer status")
        changes["status"] = status
    if data.get("usual_zone_id") is not None:
        zone = conn.execute(
            "SELECT id FROM zones WHERE id = ?", (data["usual_zone_id"],)
        ).fetchone()
        if zone is None:
            raise Invalid("unknown zone")
        changes["usual_zone_id"] = int(zone["id"])
    address = data.get("usual_address")
    if address is not None:
        cleaned = _clean(address, 300)
        if cleaned:
            changes["usual_address"] = cleaned

    if changes:
        assignments = ", ".join(f"{key} = ?" for key in changes)
        conn.execute(
            f"UPDATE customers SET {assignments} WHERE id = ?",
            (*changes.values(), customer_id),
        )
        conn.commit()

    phone = data.get("phone")
    if phone:
        customers.set_profile_fields(
            conn, customer_id, phone=phone, source="operator", confidence=1.0
        )

    note = data.get("notes")
    if note and _clean(note, 2000):
        add_note(conn, customer_id, note)

    if changes or phone or note:
        _log(conn, "customer_updated", customer_id, {"fields": sorted(changes)})
    return get_detail(conn, customer_id)


def add_tag(conn, customer_id: int, tag: str, *, reason: str | None = None) -> dict:
    conversations.get_customer(conn, customer_id)
    clean = _clean(tag, 40)
    if not clean:
        raise Invalid("tag is required")
    conn.execute(
        """INSERT OR IGNORE INTO customer_tags
           (customer_id, tag, source, reason, created_by)
           VALUES (?, ?, 'manual', ?, 'operator')""",
        (customer_id, clean, reason),
    )
    conn.commit()
    _log(conn, "customer_tag_added", customer_id, {"tag": clean})
    return {"tags": _tags(conn, [customer_id]).get(customer_id, [])}


def remove_tag(conn, customer_id: int, tag: str) -> dict:
    conversations.get_customer(conn, customer_id)
    conn.execute(
        "DELETE FROM customer_tags WHERE customer_id = ? AND tag = ?",
        (customer_id, _clean(tag, 40)),
    )
    conn.commit()
    _log(conn, "customer_tag_removed", customer_id, {"tag": tag})
    return {"tags": _tags(conn, [customer_id]).get(customer_id, [])}


def add_note(conn, customer_id: int, body: str, *, author: str = "operator") -> dict:
    conversations.get_customer(conn, customer_id)
    clean = _clean(body, 2000)
    if not clean:
        raise Invalid("note is required")
    cur = conn.execute(
        "INSERT INTO customer_notes (customer_id, body, author) VALUES (?, ?, ?)",
        (customer_id, clean, author),
    )
    conn.commit()
    _log(conn, "customer_note_added", customer_id, {"note_id": cur.lastrowid})
    return {"notes": _notes(conn, customer_id)}


def _signal_action(conn, customer_id: int, signal_id: int, action: str) -> dict:
    conversations.get_customer(conn, customer_id)
    row = conn.execute(
        "SELECT * FROM customer_signals WHERE id = ? AND customer_id = ?",
        (signal_id, customer_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"signal {signal_id} not found")
    if row["status"] != "OPEN":
        raise Invalid("signal is already resolved")
    new_status = "CONFIRMED" if action == "confirm" else "DISMISSED"
    conn.execute(
        """UPDATE customer_signals
              SET status = ?, resolved_at = ?, resolved_by = 'operator'
            WHERE id = ?""",
        (new_status, _now(), signal_id),
    )
    conn.commit()
    _log(
        conn,
        f"customer_signal_{'confirmed' if action == 'confirm' else 'dismissed'}",
        customer_id,
        {"signal_id": signal_id, "kind": row["kind"]},
    )
    return get_detail(conn, customer_id)


def confirm_signal(conn, customer_id: int, signal_id: int) -> dict:
    return _signal_action(conn, customer_id, signal_id, "confirm")


def dismiss_signal(conn, customer_id: int, signal_id: int) -> dict:
    return _signal_action(conn, customer_id, signal_id, "dismiss")


def block(conn, customer_id: int) -> dict:
    conversations.get_customer(conn, customer_id)
    conn.execute(
        "UPDATE customers SET status = 'BLOCKED' WHERE id = ?", (customer_id,)
    )
    conn.commit()
    _log(conn, "customer_blocked", customer_id)
    return get_detail(conn, customer_id)


def unblock(conn, customer_id: int) -> dict:
    conversations.get_customer(conn, customer_id)
    conn.execute(
        "UPDATE customers SET status = 'ACTIVE' WHERE id = ?", (customer_id,)
    )
    conn.commit()
    _log(conn, "customer_unblocked", customer_id)
    return get_detail(conn, customer_id)


# -------------------------------------------------------------------- signals


def _open_signal(conn, customer_id: int, kind: str):
    return conn.execute(
        """SELECT * FROM customer_signals
            WHERE customer_id = ? AND kind = ? AND status = 'OPEN'""",
        (customer_id, kind),
    ).fetchone()


def _raise_signal(
    conn,
    customer_id: int,
    kind: str,
    severity: str,
    evidence: dict,
    *,
    count: int = 1,
) -> bool:
    """Open (or refresh) a signal. True only when it is new."""
    row = _open_signal(conn, customer_id, kind)
    payload = json.dumps(evidence, ensure_ascii=False)
    stamp = _now()
    if row is None:
        conn.execute(
            """INSERT INTO customer_signals
               (customer_id, kind, severity, status, count, evidence_json,
                first_seen_at, last_seen_at)
               VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?)""",
            (customer_id, kind, severity, count, payload, stamp, stamp),
        )
        return True
    conn.execute(
        """UPDATE customer_signals
              SET count = ?, evidence_json = ?, severity = ?,
                  last_seen_at = MAX(last_seen_at, ?)
            WHERE id = ?""",
        (count, payload, severity, stamp, row["id"]),
    )
    return False


def _abuse_messages(conn, customer_id: int, *, limit: int = 10) -> list[int]:
    rows = conn.execute(
        """SELECT m.id, m.body FROM messages m
             JOIN conversations c ON c.id = m.conversation_id
            WHERE c.customer_id = ? AND m.direction = 'INBOUND'
              AND m.body IS NOT NULL
            ORDER BY m.id DESC LIMIT ?""",
        (customer_id, limit),
    ).fetchall()
    return [int(row["id"]) for row in rows if _ABUSE_RE.search(row["body"] or "")]


def scan_signals(conn, *, limit: int | None = None) -> dict:
    """Re-evaluate deterministic signals for every customer.

    Runs outside the turn (the scheduler calls it in F4) so it never adds
    latency to a reply. Only OPEN signals are written; confirming or dismissing
    stays a manual decision.
    """
    sql = "SELECT id FROM customers WHERE kind = 'customer' AND status <> 'MERGED' ORDER BY id"
    args: tuple = ()
    if limit:
        sql += " LIMIT ?"
        args = (limit,)
    customer_ids = [int(row["id"]) for row in conn.execute(sql, args).fetchall()]
    created = 0
    refreshed = 0

    for customer_id in customer_ids:
        metrics = _get_metrics(conn, customer_id)
        raised = 0

        message_ids = _abuse_messages(conn, customer_id)
        if message_ids:
            if _raise_signal(
                conn,
                customer_id,
                "abuso_verbal",
                "critical",
                {"message_ids": message_ids},
                count=len(message_ids),
            ):
                raised += 1

        if metrics["sales_cancelled"] >= 2:
            if _raise_signal(
                conn,
                customer_id,
                "cancelaciones_repetidas",
                "warning",
                {"cancelled_sales": metrics["sales_cancelled"]},
                count=metrics["sales_cancelled"],
            ):
                raised += 1

        if metrics["sales_needs_operator"] >= 2:
            if _raise_signal(
                conn,
                customer_id,
                "pedido_falso",
                "critical",
                {"needs_operator_sales": metrics["sales_needs_operator"]},
                count=metrics["sales_needs_operator"],
            ):
                raised += 1

        complaints = conn.execute(
            """SELECT COUNT(*) FROM turns t
                 JOIN conversations c ON c.id = t.conversation_id
                WHERE c.customer_id = ? AND t.intent = 'queja'""",
            (customer_id,),
        ).fetchone()[0]
        if complaints >= 3:
            if _raise_signal(
                conn,
                customer_id,
                "reclamos_repetidos",
                "warning",
                {"complaint_turns": int(complaints)},
                count=int(complaints),
            ):
                raised += 1

        if (
            metrics["turns_total"] >= 20
            and metrics["sales_settled"] == 0
            and metrics["conversations_total"] >= 2
        ):
            if _raise_signal(
                conn,
                customer_id,
                "muchas_preguntas_sin_compra",
                "info",
                {
                    "turns": metrics["turns_total"],
                    "conversations": metrics["conversations_total"],
                },
                count=metrics["turns_total"],
            ):
                raised += 1

        if (
            metrics["questions_total"] >= 15
            and metrics["sales_created"] == 0
            and not metrics.get("phone")
            and not metrics.get("usual_address")
        ):
            if _raise_signal(
                conn,
                customer_id,
                "scraping_catalogo",
                "warning",
                {"questions": metrics["questions_total"]},
                count=metrics["questions_total"],
            ):
                raised += 1

        created += raised
        refreshed += 1 if raised == 0 else 0

    conn.commit()
    return {
        "customers_scanned": len(customer_ids),
        "signals_created": created,
        "signals_refreshed": refreshed,
    }


# ---------------------------------------------------------------------- merge


def _merge_counts(conn, customer_id: int) -> dict:
    def count(sql: str) -> int:
        return int(conn.execute(sql, (customer_id,)).fetchone()[0] or 0)

    return {
        "conversations": count(
            "SELECT COUNT(*) FROM conversations WHERE customer_id = ?"
        ),
        "messages": count(
            """SELECT COUNT(*) FROM messages m
                 JOIN conversations c ON c.id = m.conversation_id
                WHERE c.customer_id = ?"""
        ),
        "sales": count("SELECT COUNT(*) FROM sales WHERE customer_id = ?"),
        "contacts": count(
            "SELECT COUNT(*) FROM customer_contacts WHERE customer_id = ?"
        ),
        "tags": count("SELECT COUNT(*) FROM customer_tags WHERE customer_id = ?"),
        "notes": count("SELECT COUNT(*) FROM customer_notes WHERE customer_id = ?"),
        "signals": count(
            "SELECT COUNT(*) FROM customer_signals WHERE customer_id = ?"
        ),
    }


def merge_preview(conn, from_id: int, into_id: int) -> dict:
    if from_id == into_id:
        raise Invalid("cannot merge a customer into itself")
    source = conversations.get_customer(conn, from_id)
    target = conversations.get_customer(conn, into_id)
    if source.get("status") == "MERGED" or target.get("status") == "MERGED":
        raise Invalid("cannot merge an already merged customer")

    shared_contacts = conn.execute(
        """SELECT a.kind, a.normalized FROM customer_contacts a
             JOIN customer_contacts b
               ON b.kind = a.kind AND b.normalized = a.normalized
            WHERE a.customer_id = ? AND b.customer_id = ?""",
        (from_id, into_id),
    ).fetchall()
    shared_tags = conn.execute(
        """SELECT a.tag FROM customer_tags a
             JOIN customer_tags b ON b.tag = a.tag
            WHERE a.customer_id = ? AND b.customer_id = ?""",
        (from_id, into_id),
    ).fetchall()
    shared_signals = conn.execute(
        """SELECT a.kind FROM customer_signals a
             JOIN customer_signals b ON b.kind = a.kind
            WHERE a.customer_id = ? AND b.customer_id = ?
              AND a.status = 'OPEN' AND b.status = 'OPEN'""",
        (from_id, into_id),
    ).fetchall()

    return {
        "from": {
            "id": from_id,
            "name": source.get("name"),
            "display_name": source.get("display_name"),
        },
        "into": {
            "id": into_id,
            "name": target.get("name"),
            "display_name": target.get("display_name"),
        },
        "move": _merge_counts(conn, from_id),
        "conflicts": {
            "contacts": [dict(row) for row in shared_contacts],
            "tags": [row["tag"] for row in shared_tags],
            "open_signals": [row["kind"] for row in shared_signals],
        },
        "irreversible": True,
    }


def merge(conn, from_id: int, into_id: int, *, reason: str | None = None) -> dict:
    preview = merge_preview(conn, from_id, into_id)
    source = conversations.get_customer(conn, from_id)
    target = conversations.get_customer(conn, into_id)
    stamp = _now()

    with conn:
        conn.execute(
            "UPDATE conversations SET customer_id = ? WHERE customer_id = ?",
            (into_id, from_id),
        )
        conn.execute(
            "UPDATE sales SET customer_id = ? WHERE customer_id = ?",
            (into_id, from_id),
        )
        conn.execute(
            "UPDATE customer_notes SET customer_id = ? WHERE customer_id = ?",
            (into_id, from_id),
        )

        # Contacts share a UNIQUE(kind, normalized): keep the target's row.
        for row in customers.contacts_for(conn, from_id):
            exists = conn.execute(
                """SELECT id FROM customer_contacts
                    WHERE customer_id = ? AND kind = ? AND normalized = ?""",
                (into_id, row["kind"], row["normalized"]),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "UPDATE customer_contacts SET customer_id = ? WHERE id = ?",
                    (into_id, row["id"]),
                )
            else:
                conn.execute(
                    "DELETE FROM customer_contacts WHERE id = ?", (row["id"],)
                )

        # Tags share UNIQUE(customer_id, tag): copy the missing ones, then drop.
        conn.execute(
            """INSERT OR IGNORE INTO customer_tags
               (customer_id, tag, source, reason, created_by, created_at)
               SELECT ?, tag, source, reason, created_by, created_at
                 FROM customer_tags WHERE customer_id = ?""",
            (into_id, from_id),
        )
        conn.execute("DELETE FROM customer_tags WHERE customer_id = ?", (from_id,))

        # Open signals share UNIQUE(customer_id, kind) WHERE status='OPEN'.
        for signal in conn.execute(
            "SELECT * FROM customer_signals WHERE customer_id = ?", (from_id,)
        ).fetchall():
            existing = conn.execute(
                """SELECT id FROM customer_signals
                    WHERE customer_id = ? AND kind = ? AND status = ?""",
                (into_id, signal["kind"], signal["status"]),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "UPDATE customer_signals SET customer_id = ? WHERE id = ?",
                    (into_id, signal["id"]),
                )
            elif signal["status"] == "OPEN":
                conn.execute(
                    """UPDATE customer_signals
                          SET count = count + ?, last_seen_at = MAX(last_seen_at, ?)
                        WHERE id = ?""",
                    (signal["count"], signal["last_seen_at"], existing["id"]),
                )
                conn.execute(
                    "DELETE FROM customer_signals WHERE id = ?", (signal["id"],)
                )
            else:
                conn.execute(
                    "DELETE FROM customer_signals WHERE id = ?", (signal["id"],)
                )

        # Fill the target's empty profile with the source's data.
        merged_profile = {
            "phone": target.get("phone") or source.get("phone"),
            "phone_normalized": target.get("phone_normalized")
            or source.get("phone_normalized"),
            "display_name": target.get("display_name") or source.get("display_name"),
            "usual_zone_id": target.get("usual_zone_id") or source.get("usual_zone_id"),
            "usual_address": target.get("usual_address") or source.get("usual_address"),
            "first_seen_at": min(
                filter(
                    None,
                    [target.get("first_seen_at"), source.get("first_seen_at")],
                ),
                default=None,
            ),
            "last_seen_at": max(
                filter(
                    None,
                    [target.get("last_seen_at"), source.get("last_seen_at")],
                ),
                default=None,
            ),
        }
        assignments = ", ".join(f"{key} = ?" for key in merged_profile)
        conn.execute(
            f"UPDATE customers SET {assignments} WHERE id = ?",
            (*merged_profile.values(), into_id),
        )
        conn.execute(
            "UPDATE customers SET status = 'MERGED', merged_into_id = ? WHERE id = ?",
            (into_id, from_id),
        )
        conn.execute(
            """INSERT INTO customer_merges
               (from_customer_id, into_customer_id, reason, created_by, created_at)
               VALUES (?, ?, ?, 'operator', ?)""",
            (from_id, into_id, _clean(reason, 500) or None, stamp),
        )

    _log(
        conn,
        "customer_merged",
        into_id,
        {"from_id": from_id, "move": preview["move"]},
    )
    return get_detail(conn, into_id)
