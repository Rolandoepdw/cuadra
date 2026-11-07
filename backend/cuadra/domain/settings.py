"""Mutable runtime settings (key/value), editable from the dashboard.

Distinct from `config` (env-driven, fixed at boot): these values live in the DB
so the operator can change them (e.g. the active LLM model) without editing
.env or restarting the process.
"""

from datetime import datetime, timezone

LLM_MODEL_KEY = "llm.model"
LLM_CTX_KEY = "llm.ctx"

BROADCAST_DEFAULT_MAX_POSTS = "broadcast.default_max_posts_per_day"
BROADCAST_DEFAULT_MIN_INTERVAL = "broadcast.default_min_interval_s"
BROADCAST_DEFAULT_MAX_INTERVAL = "broadcast.default_max_interval_s"
BROADCAST_REPEAT_PASSES = "broadcast.repeat_passes"
BROADCAST_REPEAT_WINDOW_DAYS = "broadcast.repeat_window_days"
BROADCAST_PUBLISHING = "broadcast.publishing"

CATALOG_AUTOAPPLY_PRICE = "catalog.autoapply.price"
CATALOG_AUTOAPPLY_STOCK = "catalog.autoapply.stock"
CATALOG_AUTOAPPLY_SPECS = "catalog.autoapply.specs"
CATALOG_PRICE_MAX_DELTA_PCT = "catalog.price_max_delta_pct"
CATALOG_AUTOAPPLY_MIN_CONFIDENCE = "catalog.autoapply.min_confidence"

CATALOG_AUTOAPPLY_DEFAULTS = {
    CATALOG_AUTOAPPLY_PRICE: 1,
    CATALOG_AUTOAPPLY_STOCK: 1,
    CATALOG_AUTOAPPLY_SPECS: 1,
    CATALOG_PRICE_MAX_DELTA_PCT: 50,
    CATALOG_AUTOAPPLY_MIN_CONFIDENCE: 0.85,
}

BROADCAST_DEFAULTS = {
    BROADCAST_DEFAULT_MAX_POSTS: 20,
    BROADCAST_DEFAULT_MIN_INTERVAL: 300,
    BROADCAST_DEFAULT_MAX_INTERVAL: 1200,
    BROADCAST_REPEAT_PASSES: 2,
    BROADCAST_REPEAT_WINDOW_DAYS: 1,
}


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


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def broadcast_defaults(conn) -> dict[str, int]:
    return {
        key: _as_int(get(conn, key, str(default)), default)
        for key, default in BROADCAST_DEFAULTS.items()
    }


def broadcast_publishing_enabled(conn) -> bool:
    """Global play/pause switch for campaign publishing.

    Stored in `settings` so a pause survives restarts and power cuts. One-to-one
    conversations are never affected.
    """
    return _as_int(get(conn, BROADCAST_PUBLISHING, "1"), 1) != 0


def set_broadcast_publishing(conn, enabled: bool) -> None:
    set(conn, BROADCAST_PUBLISHING, "1" if enabled else "0")


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def catalog_autoapply(conn) -> dict:
    """Automatic-apply policy for TCP catalog updates (migration 014 keys).

    Price and stock are capped by ``catalog.price_max_delta_pct``; every
    mutation also needs ``catalog.autoapply.min_confidence``. Zone changes are
    never auto-applied and are not represented here on purpose.
    """
    defaults = CATALOG_AUTOAPPLY_DEFAULTS

    def flag(key: str) -> bool:
        raw = get(conn, key, str(defaults[key]))
        return _as_int(raw, int(defaults[key])) != 0

    return {
        "price": flag(CATALOG_AUTOAPPLY_PRICE),
        "stock": flag(CATALOG_AUTOAPPLY_STOCK),
        "specs": flag(CATALOG_AUTOAPPLY_SPECS),
        "price_max_delta_pct": _as_float(
            get(conn, CATALOG_PRICE_MAX_DELTA_PCT, str(defaults[CATALOG_PRICE_MAX_DELTA_PCT])),
            float(defaults[CATALOG_PRICE_MAX_DELTA_PCT]),
        ),
        "min_confidence": _as_float(
            get(
                conn,
                CATALOG_AUTOAPPLY_MIN_CONFIDENCE,
                str(defaults[CATALOG_AUTOAPPLY_MIN_CONFIDENCE]),
            ),
            float(defaults[CATALOG_AUTOAPPLY_MIN_CONFIDENCE]),
        ),
    }


def set_broadcast_defaults(conn, data: dict) -> dict[str, int]:
    values = broadcast_defaults(conn)
    for key in BROADCAST_DEFAULTS:
        if key in data and data[key] is not None:
            values[key] = int(data[key])
    if not 0 <= values[BROADCAST_DEFAULT_MAX_POSTS] <= 1000:
        raise ValueError("max posts per day must be between 0 and 1000")
    if not 0 <= values[BROADCAST_DEFAULT_MIN_INTERVAL] <= 604800:
        raise ValueError("min interval must be between 0 and 604800 seconds")
    if not 0 <= values[BROADCAST_DEFAULT_MAX_INTERVAL] <= 604800:
        raise ValueError("max interval must be between 0 and 604800 seconds")
    if values[BROADCAST_DEFAULT_MAX_INTERVAL] < values[BROADCAST_DEFAULT_MIN_INTERVAL]:
        raise ValueError("max interval cannot be lower than min interval")
    if not 1 <= values[BROADCAST_REPEAT_PASSES] <= 100:
        raise ValueError("repeat passes must be between 1 and 100")
    if not 1 <= values[BROADCAST_REPEAT_WINDOW_DAYS] <= 30:
        raise ValueError("repeat window days must be between 1 and 30")
    for key, value in values.items():
        set(conn, key, str(value))
    return values
