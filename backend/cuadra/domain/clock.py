"""Business clock: the day boundary follows America/Havana, not UTC (H-18).

Rows store UTC timestamps as ``YYYY-MM-DD HH:MM:SS``. Dashboard counters such as
"sales today" must use the business day; when Havana is still on the previous
day (UTC-5/UTC-4) ``date('now')`` would already be tomorrow in UTC and show 0.
"""

from datetime import datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    HAVANA = ZoneInfo("America/Havana")
except Exception:  # noqa: BLE001 - no tzdata: degrade to UTC
    HAVANA = timezone.utc

_UTC_FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    """Current time in Havana."""
    return datetime.now(HAVANA)


def day_bounds(as_of: datetime | None = None) -> tuple[str, str]:
    """UTC strings for the current Havana day: ``[start, end)``."""
    moment = as_of or now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=HAVANA)
    local = moment.astimezone(HAVANA)
    start = datetime.combine(local.date(), time.min, tzinfo=HAVANA)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).strftime(_UTC_FMT),
        end.astimezone(timezone.utc).strftime(_UTC_FMT),
    )
