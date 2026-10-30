"""Per-sender Telegram pacing and rate-limit state.

Two different waits share this state:

- ``blocked_until``: a real Telegram punishment (FloodWait/PeerFlood). It is
  account-wide and is counted as a rate-limit hit by the job queue.
- ``last_action_at``: the last successful send. It drives the local human-like
  gap, which is Cuadra's own pacing and must never be treated as a failure.

Keeping both in SQLite means a restart cannot reset the cooldown or resume
sending too fast. This module is the single source of truth for "when may this
sender act next"; the scheduler, the queue and the adapters all ask it instead
of duplicating the rule.

Bookkeeping is best-effort on purpose: a local database hiccup must not turn a
successful Telegram send into a failed job (which would resend it) nor stop the
worker. Real cooldowns still come from Telegram on the next attempt.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from cuadra import db

log = logging.getLogger("cuadra.rate_limit")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get(sender: str) -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM sender_state WHERE sender = ?", (sender,)
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def _next_at(
    state: dict, min_gap: float, now: datetime
) -> tuple[datetime, datetime | None, datetime | None]:
    """Return (next_action_at, blocked_until, last_action_at) in UTC."""
    next_at = now
    blocked = _parse(state.get("blocked_until"))
    if blocked is not None:
        next_at = max(next_at, blocked)
    last = _parse(state.get("last_action_at"))
    if last is not None and min_gap > 0:
        next_at = max(next_at, last + timedelta(seconds=float(min_gap)))
    return next_at, blocked, last


def remaining(sender: str, min_gap: float = 0.0) -> float:
    """Seconds until this sender may act again (0 means ready now).

    Includes both the local gap and any real Telegram cooldown. Callers that
    need to tell them apart use ``local_gap_remaining``/``cooldown_remaining``.
    """
    try:
        state = _get(sender)
    except sqlite3.Error:
        log.warning("could not read sender_state for %s", sender, exc_info=True)
        return 0.0
    now = _now()
    next_at, _blocked, _last = _next_at(state, min_gap, now)
    return max(0.0, (next_at - now).total_seconds())


def local_gap_remaining(sender: str, min_gap: float) -> float:
    """Seconds left in the local human-like gap (never a Telegram punishment)."""
    if min_gap <= 0:
        return 0.0
    try:
        state = _get(sender)
    except sqlite3.Error:
        log.warning("could not read sender_state for %s", sender, exc_info=True)
        return 0.0
    now = _now()
    last = _parse(state.get("last_action_at"))
    if last is None:
        return 0.0
    end = last + timedelta(seconds=float(min_gap))
    return max(0.0, (end - now).total_seconds())


def cooldown_remaining(sender: str) -> float:
    """Seconds left in a real Telegram cooldown (0 means ready now)."""
    try:
        state = _get(sender)
    except sqlite3.Error:
        log.warning("could not read sender_state for %s", sender, exc_info=True)
        return 0.0
    now = _now()
    blocked = _parse(state.get("blocked_until"))
    if blocked is None:
        return 0.0
    return max(0.0, (blocked - now).total_seconds())


def snapshot(sender: str, min_gap: float = 0.0) -> dict:
    """Small, JSON-friendly view of the sender for the health panel."""
    try:
        state = _get(sender)
    except sqlite3.Error:
        log.warning("could not read sender_state for %s", sender, exc_info=True)
        state = {}
    now = _now()
    next_at, blocked, last = _next_at(state, min_gap, now)
    wait = max(0.0, (next_at - now).total_seconds())

    if blocked is not None and blocked > now and blocked >= next_at:
        reason = "cooldown"
    elif (
        last is not None
        and min_gap > 0
        and last + timedelta(seconds=float(min_gap)) > now
    ):
        reason = "gap"
    else:
        reason = "ready"

    return {
        "sender": sender,
        "min_gap_s": float(min_gap),
        "last_action_at": last.isoformat() if last else None,
        "blocked_until": blocked.isoformat() if blocked else None,
        "next_action_at": next_at.isoformat(),
        "wait_s": round(wait, 1),
        "reason": reason,
    }


def mark_action(sender: str) -> None:
    """Record a successful send (used for the human-like global gap)."""
    now = _now().isoformat()
    try:
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO sender_state (sender, last_action_at, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(sender) DO UPDATE SET
                       last_action_at = excluded.last_action_at,
                       updated_at = excluded.updated_at""",
                (sender, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        log.warning("could not record action for %s", sender, exc_info=True)


def set_cooldown(sender: str, seconds: float, reason: str | None = None) -> None:
    """Block the sender for at least `seconds`, never shortening a longer wait."""
    seconds = max(0.0, float(seconds or 0))
    if seconds <= 0:
        return
    now = _now()
    until = now + timedelta(seconds=seconds)
    try:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT blocked_until FROM sender_state WHERE sender = ?", (sender,)
            ).fetchone()
            current = _parse(row["blocked_until"]) if row is not None else None
            if current is not None and current > until:
                until = current
            conn.execute(
                """INSERT INTO sender_state
                       (sender, blocked_until, reason, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(sender) DO UPDATE SET
                       blocked_until = excluded.blocked_until,
                       reason = excluded.reason,
                       updated_at = excluded.updated_at""",
                (sender, until.isoformat(), reason, now.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        log.warning("could not set cooldown for %s", sender, exc_info=True)
