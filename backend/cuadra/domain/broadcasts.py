"""Broadcast domain: groups, categories, campaigns, spintax and target selection.

Key rules (docs 02 and 04, Hito 7):
- Manual group selection (`broadcast_group_links`) overrides automatic matching.
- In automatic mode a product with a category reaches groups that list that
  category (or unrestricted groups); a product WITHOUT category only reaches
  unrestricted groups. It never lands in a group with restrictions.
- Each group enforces `max_posts_per_day` (America/Havana) and
  `min_interval_s`/`max_interval_s`; the scheduler dispatches one post per tick.
- Campaigns recur: every `repeat_window_days` each campaign is published
  `repeat_passes` times per target group, subject to the group limits.
"""

import logging
import random
import re
from datetime import datetime, time, timedelta, timezone
from sqlite3 import IntegrityError

from cuadra import config
from cuadra.domain import catalog, events, settings
from cuadra.domain.errors import Invalid, NotFound

log = logging.getLogger("cuadra.broadcasts")

CHANNELS = ("telegram", "whatsapp")
GROUP_STATUSES = ("ACTIVE", "PAUSED", "BLOCKED")
SENDERS = ("bot", "userbot")
SPINTAX_MAX_PASSES = 30


_SPINTAX = re.compile(r"\{([^{}]*\|[^{}]*)\}")


# --------------------------------------------------------------------- utils

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _havana_day_start_utc() -> str:
    """Midnight in America/Havana expressed in UTC (for post counters)."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/Havana"))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - no tzdata: fall back to UTC day
        return datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")


def _cycle_bounds(days: int) -> tuple[str, str]:
    """UTC start/end of the current publication cycle in America/Havana."""
    days = max(1, int(days))
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Havana")
    except Exception:  # noqa: BLE001 - no tzdata: use UTC days
        tz = timezone.utc
    now = datetime.now(tz)
    anchor = datetime(2024, 1, 1, tzinfo=tz)
    offset = (now.date() - anchor.date()).days % days
    start = datetime.combine(now.date() - timedelta(days=offset), time.min, tzinfo=tz)
    end = start + timedelta(days=days)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.astimezone(timezone.utc).strftime(fmt), end.astimezone(timezone.utc).strftime(fmt)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Invalid(message)


def _clean_text(value, field: str, *, max_len: int = 4096) -> str:
    text = (value or "").strip()
    _require(bool(text), f"'{field}' is required")
    _require(len(text) <= max_len, f"'{field}' must be at most {max_len} characters")
    return text


def _int(value, field: str, *, minimum: int = 0, maximum: int = 10080) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise Invalid(f"'{field}' must be an integer") from exc
    _require(minimum <= number <= maximum, f"'{field}' must be between {minimum} and {maximum}")
    return number


# -------------------------------------------------------------------- groups

_GROUP_SELECT = """
    SELECT g.*,
           (SELECT GROUP_CONCAT(gc.category_id)
            FROM group_categories gc WHERE gc.group_id = g.id) AS category_ids,
           (SELECT GROUP_CONCAT(gz.zone_id)
            FROM group_zones gz WHERE gz.group_id = g.id) AS zone_ids
    FROM broadcast_groups g
"""


def _group_out(row) -> dict:
    group = dict(row)
    categories = group.pop("category_ids", None)
    zones = group.pop("zone_ids", None)
    group["category_ids"] = [int(v) for v in (categories or "").split(",") if v]
    group["zone_ids"] = [int(v) for v in (zones or "").split(",") if v]
    return group


def list_groups(conn, *, channel: str | None = None, status: str | None = None) -> list[dict]:
    where, args = [], []
    if channel:
        where.append("g.channel = ?")
        args.append(channel)
    if status:
        where.append("g.status = ?")
        args.append(status)
    sql = _GROUP_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [_group_out(r) for r in conn.execute(sql + " ORDER BY g.name", args)]


def get_group(conn, group_id: int) -> dict:
    row = conn.execute(_GROUP_SELECT + " WHERE g.id = ?", (group_id,)).fetchone()
    if row is None:
        raise NotFound(f"group {group_id} not found")
    return _group_out(row)


def _validate_categories(conn, category_ids: list[int] | None) -> list[int]:
    ids = [int(v) for v in (category_ids or [])]
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    found = {
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM categories WHERE id IN ({placeholders})", ids
        )
    }
    missing = [v for v in ids if v not in found]
    _require(not missing, f"unknown category id(s): {missing}")
    return sorted(set(ids))


def _set_categories(conn, group_id: int, category_ids: list[int]) -> None:
    conn.execute("DELETE FROM group_categories WHERE group_id = ?", (group_id,))
    conn.executemany(
        "INSERT INTO group_categories (group_id, category_id) VALUES (?, ?)",
        [(group_id, cid) for cid in category_ids],
    )


def _validate_zones(conn, zone_ids: list[int] | None) -> list[int]:
    ids = [int(v) for v in (zone_ids or [])]
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    found = {
        r["id"]
        for r in conn.execute(f"SELECT id FROM zones WHERE id IN ({placeholders})", ids)
    }
    missing = [v for v in ids if v not in found]
    _require(not missing, f"unknown zone id(s): {missing}")
    return sorted(set(ids))


def _set_zones(conn, group_id: int, zone_ids: list[int]) -> None:
    conn.execute("DELETE FROM group_zones WHERE group_id = ?", (group_id,))
    conn.executemany(
        "INSERT INTO group_zones (group_id, zone_id) VALUES (?, ?)",
        [(group_id, zid) for zid in zone_ids],
    )


def _validate_group(conn, data: dict, *, partial: bool) -> dict:
    fields: dict = {}
    if "channel" in data:
        _require(data["channel"] in CHANNELS, f"'channel' must be one of {CHANNELS}")
        fields["channel"] = data["channel"]
    elif not partial:
        fields["channel"] = "telegram"

    if "target" in data:
        target = _clean_text(data["target"], "target", max_len=255)
        fields["target"] = target
    elif not partial:
        raise Invalid("'target' is required")

    if "name" in data:
        fields["name"] = _clean_text(data["name"], "name", max_len=120)
    elif not partial:
        raise Invalid("'name' is required")

    if data.get("sender") is not None:
        _require(data["sender"] in SENDERS, f"'sender' must be one of {SENDERS}")
        fields["sender"] = data["sender"]
    if data.get("status") is not None:
        _require(data["status"] in GROUP_STATUSES, f"'status' must be one of {GROUP_STATUSES}")
        fields["status"] = data["status"]

    if data.get("max_posts_per_day") is not None:
        fields["max_posts_per_day"] = _int(
            data["max_posts_per_day"], "max_posts_per_day", maximum=1000
        )
    if data.get("min_interval_s") is not None:
        fields["min_interval_s"] = _int(
            data["min_interval_s"], "min_interval_s", maximum=604800
        )
    if data.get("max_interval_s") is not None:
        fields["max_interval_s"] = _int(
            data["max_interval_s"], "max_interval_s", maximum=604800
        )
    lo = fields.get("min_interval_s")
    hi = fields.get("max_interval_s")
    if lo is not None and hi is not None:
        _require(lo <= hi, "'min_interval_s' cannot be greater than 'max_interval_s'")
    if "notes" in data:
        fields["notes"] = data["notes"]
    return fields


def create_group(conn, data: dict) -> dict:
    fields = _validate_group(conn, data, partial=False)
    defaults = settings.broadcast_defaults(conn)
    fields.setdefault("sender", "userbot")
    fields.setdefault("status", "ACTIVE")
    fields.setdefault(
        "max_posts_per_day", defaults[settings.BROADCAST_DEFAULT_MAX_POSTS]
    )
    fields.setdefault(
        "min_interval_s", defaults[settings.BROADCAST_DEFAULT_MIN_INTERVAL]
    )
    fields.setdefault(
        "max_interval_s", defaults[settings.BROADCAST_DEFAULT_MAX_INTERVAL]
    )
    fields.setdefault("notes", None)
    category_ids = _validate_categories(conn, data.get("category_ids"))
    zone_ids = _validate_zones(conn, data.get("zone_ids"))

    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    try:
        cur = conn.execute(
            f"INSERT INTO broadcast_groups ({cols}) VALUES ({marks})", tuple(fields.values())
        )
    except IntegrityError as exc:
        raise Invalid("this channel/target is already registered") from exc
    _set_categories(conn, cur.lastrowid, category_ids)
    _set_zones(conn, cur.lastrowid, zone_ids)
    events.log(
        conn,
        "dashboard",
        "broadcast_group_created",
        entity="broadcast_groups",
        entity_id=cur.lastrowid,
        details={
            "name": fields["name"],
            "target": fields["target"],
            "sender": fields.get("sender", "userbot"),
        },
    )
    conn.commit()
    return get_group(conn, cur.lastrowid)


def update_group(conn, group_id: int, data: dict) -> dict:
    group = get_group(conn, group_id)
    fields = _validate_group(conn, data, partial=True)
    category_ids = (
        _validate_categories(conn, data.get("category_ids"))
        if "category_ids" in data
        else None
    )
    zone_ids = (
        _validate_zones(conn, data.get("zone_ids")) if "zone_ids" in data else None
    )
    _require(
        bool(fields) or category_ids is not None or zone_ids is not None,
        "nothing to update",
    )
    if fields:
        clause = ", ".join(f"{k} = ?" for k in fields)
        try:
            conn.execute(
                f"UPDATE broadcast_groups SET {clause}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), group_id),
            )
        except IntegrityError as exc:
            raise Invalid("this channel/target is already registered") from exc
    if category_ids is not None:
        _set_categories(conn, group_id, category_ids)
    if zone_ids is not None:
        _set_zones(conn, group_id, zone_ids)
    events.log(
        conn,
        "dashboard",
        "broadcast_group_updated",
        entity="broadcast_groups",
        entity_id=group_id,
        details={"name": group["name"], "fields": sorted(data)},
    )
    conn.commit()
    return get_group(conn, group_id)


def apply_strategy_to_all(conn, data: dict) -> dict:
    """Persist global broadcast defaults and apply them to every group."""
    values = settings.set_broadcast_defaults(conn, data)
    cur = conn.execute(
        """UPDATE broadcast_groups
           SET max_posts_per_day = ?, min_interval_s = ?, max_interval_s = ?,
               updated_at = ?
           WHERE 1 = 1""",
        (
            values[settings.BROADCAST_DEFAULT_MAX_POSTS],
            values[settings.BROADCAST_DEFAULT_MIN_INTERVAL],
            values[settings.BROADCAST_DEFAULT_MAX_INTERVAL],
            _now(),
        ),
    )
    events.log(
        conn,
        "dashboard",
        "broadcast_strategy_applied",
        entity="settings",
        details={"groups": cur.rowcount, **values},
    )
    conn.commit()
    return {"groups": cur.rowcount, **values}


def delete_group(conn, group_id: int) -> dict:
    group = get_group(conn, group_id)
    used = conn.execute(
        "SELECT COUNT(*) FROM broadcast_posts WHERE group_id = ?", (group_id,)
    ).fetchone()[0]
    _require(
        used == 0,
        f"group has {used} post(s); keep the history and set status BLOCKED instead",
    )
    conn.execute("DELETE FROM broadcast_groups WHERE id = ?", (group_id,))
    events.log(
        conn,
        "dashboard",
        "broadcast_group_deleted",
        entity="broadcast_groups",
        entity_id=group_id,
        details={"name": group["name"]},
    )
    conn.commit()
    return {"ok": True, "id": group_id, "deleted": True}


# ---------------------------------------------------------------- broadcasts

_BROADCAST_SELECT = """
    SELECT b.*,
           (SELECT COUNT(*) FROM broadcast_posts p
            WHERE p.broadcast_id = b.id AND p.status = 'SENT') AS posts_sent,
           (SELECT COUNT(*) FROM broadcast_posts p
            WHERE p.broadcast_id = b.id AND p.status = 'FAILED') AS posts_failed,
           (SELECT COUNT(*) FROM broadcast_group_links l
            WHERE l.broadcast_id = b.id) AS group_count
    FROM broadcasts b
"""


def _broadcast_out(conn, row) -> dict:
    broadcast = dict(row)
    broadcast["group_ids"] = [
        r["group_id"]
        for r in conn.execute(
            "SELECT group_id FROM broadcast_group_links WHERE broadcast_id = ? ORDER BY group_id",
            (broadcast["id"],),
        )
    ]
    return broadcast


def list_broadcasts(conn, *, status: str | None = None) -> list[dict]:
    sql, args = _BROADCAST_SELECT, []
    if status:
        sql += " WHERE b.status = ?"
        args.append(status)
    return [
        _broadcast_out(conn, r)
        for r in conn.execute(sql + " ORDER BY b.id DESC", args)
    ]


def get_broadcast(conn, broadcast_id: int) -> dict:
    row = conn.execute(_BROADCAST_SELECT + " WHERE b.id = ?", (broadcast_id,)).fetchone()
    if row is None:
        raise NotFound(f"broadcast {broadcast_id} not found")
    return _broadcast_out(conn, row)


def create_broadcast(conn, data: dict) -> dict:
    template = _clean_text(data.get("template"), "template")
    channel = data.get("channel") or "telegram"
    _require(channel in CHANNELS, f"'channel' must be one of {CHANNELS}")
    product_id = data.get("product_id")
    if product_id is not None:
        catalog.get_product(conn, int(product_id))

    group_ids = [int(v) for v in (data.get("group_ids") or [])]
    if group_ids:
        placeholders = ", ".join("?" for _ in group_ids)
        rows = conn.execute(
            f"SELECT id, channel FROM broadcast_groups WHERE id IN ({placeholders})",
            group_ids,
        ).fetchall()
        found = {r["id"]: r["channel"] for r in rows}
        missing = [v for v in group_ids if v not in found]
        _require(not missing, f"unknown group id(s): {missing}")
        wrong = [v for v in group_ids if found[v] != channel]
        _require(not wrong, f"group(s) {wrong} do not belong to channel '{channel}'")

    cur = conn.execute(
        """INSERT INTO broadcasts (channel, template, product_id, status, next_send_at)
           VALUES (?, ?, ?, 'SCHEDULED', ?)""",
        (channel, template, product_id, _now()),
    )
    broadcast_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO broadcast_group_links (broadcast_id, group_id) VALUES (?, ?)",
        [(broadcast_id, gid) for gid in group_ids],
    )
    events.log(
        conn,
        "dashboard",
        "broadcast_created",
        entity="broadcasts",
        entity_id=broadcast_id,
        details={"channel": channel, "product_id": product_id, "groups": len(group_ids)},
    )
    conn.commit()
    return get_broadcast(conn, broadcast_id)


def cancel_broadcast(conn, broadcast_id: int) -> dict:
    broadcast = get_broadcast(conn, broadcast_id)
    _require(
        broadcast["status"] in ("SCHEDULED", "SENDING"),
        f"cannot cancel a {broadcast['status']} broadcast",
    )
    conn.execute(
        """UPDATE broadcasts
           SET status = 'CANCELLED', next_send_at = NULL, updated_at = ?
           WHERE id = ?""",
        (_now(), broadcast_id),
    )
    events.log(
        conn,
        "dashboard",
        "broadcast_cancelled",
        entity="broadcasts",
        entity_id=broadcast_id,
    )
    conn.commit()
    return get_broadcast(conn, broadcast_id)


def send_now(conn, broadcast_id: int) -> dict:
    broadcast = get_broadcast(conn, broadcast_id)
    _require(
        broadcast["status"] in ("SCHEDULED", "SENDING", "FAILED"),
        f"cannot resend a {broadcast['status']} broadcast",
    )
    if broadcast["status"] == "FAILED":
        conn.execute(
            "DELETE FROM broadcast_posts WHERE broadcast_id = ? AND status = 'FAILED'",
            (broadcast_id,),
        )
    conn.execute(
        """UPDATE broadcasts
           SET status = 'SCHEDULED', next_send_at = ?, updated_at = ?
           WHERE id = ?""",
        (_now(), _now(), broadcast_id),
    )
    events.log(
        conn,
        "dashboard",
        "broadcast_send_now",
        entity="broadcasts",
        entity_id=broadcast_id,
    )
    conn.commit()
    return get_broadcast(conn, broadcast_id)


def update_broadcast(conn, broadcast_id: int, data: dict) -> dict:
    broadcast = get_broadcast(conn, broadcast_id)
    fields: dict = {}
    if "template" in data:
        fields["template"] = _clean_text(data["template"], "template")
    if "product_id" in data:
        if data["product_id"] is None:
            fields["product_id"] = None
        else:
            product_id = int(data["product_id"])
            catalog.get_product(conn, product_id)
            fields["product_id"] = product_id
    if "channel" in data and data["channel"] is not None:
        _require(data["channel"] in CHANNELS, f"'channel' must be one of {CHANNELS}")
        fields["channel"] = data["channel"]
    if fields:
        clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE broadcasts SET {clause}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), broadcast_id),
        )
    group_ids: list[int] = []
    if "group_ids" in data:
        group_ids = [int(v) for v in (data.get("group_ids") or [])]
        if group_ids:
            placeholders = ", ".join("?" for _ in group_ids)
            rows = conn.execute(
                f"SELECT id, channel FROM broadcast_groups WHERE id IN ({placeholders})",
                group_ids,
            ).fetchall()
            found = {r["id"]: r["channel"] for r in rows}
            missing = [v for v in group_ids if v not in found]
            _require(not missing, f"unknown group id(s): {missing}")
            wrong = [v for v in group_ids if found[v] != (broadcast.get("channel") or "telegram")]
            _require(
                not wrong,
                f"group(s) {wrong} do not belong to channel '{broadcast.get('channel') or 'telegram'}'",
            )
        conn.execute(
            "DELETE FROM broadcast_group_links WHERE broadcast_id = ?",
            (broadcast_id,),
        )
        conn.executemany(
            "INSERT INTO broadcast_group_links (broadcast_id, group_id) VALUES (?, ?)",
            [(broadcast_id, gid) for gid in group_ids],
        )
    events.log(
        conn,
        "dashboard",
        "broadcast_updated",
        entity="broadcasts",
        entity_id=broadcast_id,
        details={"fields": sorted(data), "group_ids": len(group_ids)},
    )
    conn.commit()
    return get_broadcast(conn, broadcast_id)


def delete_broadcast(conn, broadcast_id: int) -> dict:
    broadcast = get_broadcast(conn, broadcast_id)
    conn.execute("DELETE FROM broadcast_group_links WHERE broadcast_id = ?", (broadcast_id,))
    conn.execute("DELETE FROM broadcasts WHERE id = ?", (broadcast_id,))
    events.log(
        conn,
        "dashboard",
        "broadcast_deleted",
        entity="broadcasts",
        entity_id=broadcast_id,
        details={"channel": broadcast["channel"], "groups": broadcast["group_count"]},
    )
    conn.commit()
    return {"ok": True, "id": broadcast_id, "deleted": True}


def reevaluate_all(conn) -> dict:
    """Re-check every active campaign ignoring the `next_send_at` gate.

    Used by the dashboard "Reevaluar" action: after the strategy changes (for
    example raising `repeat_passes`) campaigns parked until the end of the cycle
    are re-examined immediately, so an available slot is published now instead of
    waiting for the window to reset. Campaigns with no capacity are re-parked.
    """
    summary = {"checked": 0, "queued": 0, "waiting": 0, "failed": 0, "skipped": 0}
    rows = conn.execute(
        """SELECT * FROM broadcasts
           WHERE status IN ('SCHEDULED', 'SENDING')
           ORDER BY id"""
    ).fetchall()
    for row in rows:
        broadcast = dict(row)
        summary["checked"] += 1
        try:
            outcome = schedule_next(conn, broadcast)
        except Exception:  # noqa: BLE001 - one bad campaign must not stop the sweep
            conn.rollback()
            log.exception("broadcast %s reevaluation failed", broadcast["id"])
            outcome = "skipped"
        summary[outcome] = summary.get(outcome, 0) + 1
    events.log(
        conn,
        "dashboard",
        "broadcasts_reevaluated",
        entity="settings",
        details=summary,
    )
    conn.commit()
    return summary


def list_posts(conn, broadcast_id: int) -> list[dict]:
    get_broadcast(conn, broadcast_id)
    rows = conn.execute(
        """SELECT p.*, g.name AS group_name, g.target AS group_target,
                  g.sender AS group_sender
           FROM broadcast_posts p
           JOIN broadcast_groups g ON g.id = p.group_id
           WHERE p.broadcast_id = ?
           ORDER BY p.id""",
        (broadcast_id,),
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------- target logic

def _product_context(conn, product_id: int | None) -> tuple[dict, dict | None]:
    if product_id is None:
        return (
            {
                "id": "",
                "producto": "",
                "precio": "",
                "moneda": "",
                "zona": "",
                "proveedor": "",
                "categoria": "",
            },
            None,
        )
    product = catalog.get_product(conn, int(product_id))
    supplier = catalog.get_supplier(conn, product["supplier_id"])
    category = None
    if product.get("category_id"):
        category = conn.execute(
            "SELECT name FROM categories WHERE id = ?", (product["category_id"],)
        ).fetchone()
    price = product["price"]
    supplier_zones = catalog.get_supplier_delivery_zones(conn, product["supplier_id"])
    delivery_zone = ", ".join(z["name"] for z in supplier_zones) if supplier_zones else "Todas"
    return (
        {
            "id": str(product["id"]),
            "producto": product["name"],
            "precio": f"{price:.2f}".rstrip("0").rstrip("."),
            "moneda": product["currency"],
            "zona": delivery_zone,
            "proveedor": supplier.get("business_alias") or supplier["name"],
            "categoria": category["name"] if category else "",
        },
        product,
    )


def render_template(template: str, context: dict, rng: random.Random | None = None) -> str:
    """Resolve spintax `{a|b}` first, then `{producto}`-style tokens."""
    rng = rng or random
    text = template
    for _ in range(SPINTAX_MAX_PASSES):
        resolved = _SPINTAX.sub(lambda m: rng.choice(m.group(1).split("|")), text)
        if resolved == text:
            break
        text = resolved
    for key, value in context.items():
        text = text.replace("{" + key + "}", str(value))
    return text.strip()


def _manual_targets(conn, broadcast: dict) -> list[dict]:
    rows = conn.execute(
        """SELECT g.*,
                  (SELECT GROUP_CONCAT(gc.category_id)
                   FROM group_categories gc WHERE gc.group_id = g.id) AS category_ids
           FROM broadcast_groups g
           JOIN broadcast_group_links l ON l.group_id = g.id
           WHERE l.broadcast_id = ? AND g.channel = ? AND g.status = 'ACTIVE'
           ORDER BY g.id""",
        (broadcast["id"], broadcast["channel"]),
    )
    return [_group_out(r) for r in rows]


def _auto_targets(conn, broadcast: dict) -> list[dict]:
    rows = conn.execute(
        _GROUP_SELECT + " WHERE g.channel = ? AND g.status = 'ACTIVE' ORDER BY g.id",
        (broadcast["channel"],),
    )
    product = None
    if broadcast.get("product_id"):
        product = catalog.get_product(conn, int(broadcast["product_id"]))
    groups = []
    for row in rows:
        group = _group_out(row)
        cats = set(group["category_ids"])
        if product is not None:
            cid = product.get("category_id")
            if cid is None:
                if cats:
                    continue
            elif cats and cid not in cats:
                continue
        groups.append(group)
    return groups


def eligible_groups(conn, broadcast: dict) -> list[dict]:
    """Groups the campaign may target, ignoring per-day/interval limits."""
    manual = conn.execute(
        "SELECT 1 FROM broadcast_group_links WHERE broadcast_id = ? LIMIT 1",
        (broadcast["id"],),
    ).fetchone()
    return _manual_targets(conn, broadcast) if manual else _auto_targets(conn, broadcast)


def _cycle_counts(conn, broadcast_id: int, window_start: str) -> dict[int, int]:
    return {
        r["group_id"]: r["total"]
        for r in conn.execute(
            """SELECT group_id, COUNT(*) AS total FROM broadcast_posts
               WHERE broadcast_id = ? AND sent_at >= ?
               GROUP BY group_id""",
            (broadcast_id, window_start),
        )
    }


def _sent_today(conn, group_id: int) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM broadcast_posts
           WHERE group_id = ? AND status IN ('QUEUED', 'SENT') AND sent_at >= ?""",
        (group_id, _havana_day_start_utc()),
    ).fetchone()[0]


def _last_sent(conn, group_id: int) -> datetime | None:
    row = conn.execute(
        """SELECT MAX(sent_at) AS last FROM broadcast_posts
           WHERE group_id = ? AND status IN ('QUEUED', 'SENT')""",
        (group_id,),
    ).fetchone()
    return _parse_utc(row["last"]) if row else None


def _interval(group: dict) -> tuple[int, int]:
    lo = max(0, int(group.get("min_interval_s") or 0))
    hi = max(lo, int(group.get("max_interval_s") or 0))
    return lo, hi


def _next_day_start() -> datetime:
    start = _parse_utc(_havana_day_start_utc())
    return (start or datetime.now(timezone.utc)) + timedelta(days=1)


def _retry_at(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def schedule_next(conn, broadcast: dict) -> str:
    """Queue one post for a due campaign inside the current recurrence cycle."""
    broadcast_id = broadcast["id"]
    if broadcast["status"] not in ("SCHEDULED", "SENDING"):
        return "skipped"

    targets = eligible_groups(conn, broadcast)
    if not targets:
        conn.execute(
            """UPDATE broadcasts SET status = 'FAILED', next_send_at = NULL, updated_at = ?
               WHERE id = ?""",
            (_now(), broadcast_id),
        )
        events.log(
            conn,
            "scheduler",
            "broadcast_no_targets",
            entity="broadcasts",
            entity_id=broadcast_id,
        )
        conn.commit()
        return "failed"

    defaults = settings.broadcast_defaults(conn)
    passes = int(defaults[settings.BROADCAST_REPEAT_PASSES])
    window_start, window_end = _cycle_bounds(
        defaults[settings.BROADCAST_REPEAT_WINDOW_DAYS]
    )
    counts = _cycle_counts(conn, broadcast_id, window_start)
    pending = [group for group in targets if counts.get(group["id"], 0) < passes]

    if not pending:
        conn.execute(
            """UPDATE broadcasts SET status = 'SCHEDULED', next_send_at = ?, updated_at = ?
               WHERE id = ?""",
            (window_end, _now(), broadcast_id),
        )
        events.log(
            conn,
            "scheduler",
            "broadcast_cycle_complete",
            entity="broadcasts",
            entity_id=broadcast_id,
            details={"window_start": window_start, "passes": passes},
        )
        conn.commit()
        return "waiting"

    now = datetime.now(timezone.utc)
    candidates: list[tuple[datetime | None, dict]] = []
    next_times: list[datetime] = []
    for group in pending:
        cap = int(group.get("max_posts_per_day") or 0)
        if cap > 0 and _sent_today(conn, group["id"]) >= cap:
            next_times.append(_next_day_start())
            continue
        last = _last_sent(conn, group["id"])
        lo = int(group.get("min_interval_s") or 0)
        if last is not None and now - last < timedelta(seconds=lo):
            next_times.append(last + timedelta(seconds=lo))
            continue
        candidates.append((last, group))

    if not candidates:
        next_at = min(next_times) if next_times else now + timedelta(seconds=60)
        next_at = max(next_at, now + timedelta(seconds=1))
        conn.execute(
            """UPDATE broadcasts SET status = 'SCHEDULED', next_send_at = ?, updated_at = ?
               WHERE id = ?""",
            (next_at.strftime("%Y-%m-%d %H:%M:%S"), _now(), broadcast_id),
        )
        conn.commit()
        return "waiting"

    fresh = [item for item in candidates if item[0] is None]
    last, group = random.choice(fresh) if fresh else min(candidates, key=lambda item: item[0])

    context, product = _product_context(conn, broadcast.get("product_id"))
    text = render_template(broadcast["template"], context)
    if product is not None and "{id}" not in broadcast["template"]:
        text = f"{text}\n\nID: {product['id']}"
    image_path = product.get("main_image") if product else None

    cur = conn.execute(
        """INSERT INTO broadcast_posts
           (broadcast_id, group_id, channel, sent_text, image_path, status,
            attempts, sent_at, window_start)
           VALUES (?, ?, ?, ?, ?, 'QUEUED', 0, ?, ?)""",
        (
            broadcast_id,
            group["id"],
            group["channel"],
            text,
            image_path,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            window_start,
        ),
    )
    post_id = cur.lastrowid

    lo, hi = _interval(group)
    delay_s = random.randint(lo, hi) if hi > 0 else 0
    next_at = now + timedelta(seconds=delay_s)
    cycle_end = _parse_utc(window_end)
    if cycle_end is not None and next_at > cycle_end:
        next_at = cycle_end
    conn.execute(
        """UPDATE broadcasts SET status = 'SCHEDULED', next_send_at = ?, updated_at = ?
           WHERE id = ?""",
        (next_at.strftime("%Y-%m-%d %H:%M:%S"), _now(), broadcast_id),
    )
    events.log(
        conn,
        "scheduler",
        "broadcast_scheduled",
        entity="broadcasts",
        entity_id=broadcast_id,
        details={
            "post_id": post_id,
            "group_id": group["id"],
            "group_name": group["name"],
            "sender": group["sender"],
            "next_send_at": next_at.strftime("%Y-%m-%d %H:%M:%S"),
            "window_start": window_start,
            "pass": counts.get(group["id"], 0) + 1,
            "passes": passes,
            "last_sent": last.strftime("%Y-%m-%d %H:%M:%S") if last else None,
        },
    )
    conn.commit()

    from cuadra.ai import queue

    queue.enqueue(
        "send_broadcast",
        {
            "post_id": post_id,
            "broadcast_id": broadcast_id,
            "group_id": group["id"],
            "text": text,
            "image_path": image_path,
        },
        priority=config.PRIORITY_BROADCAST,
    )
    return "queued"


def record_result(conn, post_id: int, status: str, error: str | None = None) -> None:
    """Record the outcome of a queued post without creating a duplicate row."""
    _require(status in ("SENT", "FAILED"), "invalid post status")
    row = conn.execute(
        """SELECT p.broadcast_id, p.group_id, g.name AS group_name
           FROM broadcast_posts p
           LEFT JOIN broadcast_groups g ON g.id = p.group_id
           WHERE p.id = ?""",
        (post_id,),
    ).fetchone()
    _require(row is not None, f"broadcast post {post_id} not found")
    conn.execute(
        """UPDATE broadcast_posts
           SET status = ?, error = ?, attempts = attempts + 1, sent_at = ?
           WHERE id = ?""",
        (status, error, _now(), post_id),
    )
    events.log(
        conn,
        "bot" if status == "SENT" else "scheduler",
        "broadcast_posted" if status == "SENT" else "broadcast_post_failed",
        entity="broadcasts",
        entity_id=row["broadcast_id"],
        details={
            "post_id": post_id,
            "group_id": row["group_id"],
            "group_name": row["group_name"],
            "status": status,
            "error": error,
        },
    )
    conn.commit()


def due_broadcasts(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM broadcasts
               WHERE status IN ('SCHEDULED', 'SENDING')
                 AND (next_send_at IS NULL OR next_send_at <= ?)
               ORDER BY id""",
            (_now(),),
        )
    ]


def unposted_targets(conn, broadcast: dict) -> list[dict]:
    defaults = settings.broadcast_defaults(conn)
    window_start, _ = _cycle_bounds(defaults[settings.BROADCAST_REPEAT_WINDOW_DAYS])
    counts = _cycle_counts(conn, broadcast["id"], window_start)
    passes = int(defaults[settings.BROADCAST_REPEAT_PASSES])
    return [
        group
        for group in eligible_groups(conn, broadcast)
        if counts.get(group["id"], 0) < passes
    ]
