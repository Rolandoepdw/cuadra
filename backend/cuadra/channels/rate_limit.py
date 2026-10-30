"""Per-account Telegram rate-limit state.

Telegram FloodWait is account-wide: a wait triggered by one group must stop
every action of that sender. The state lives in SQLite so a core restart cannot
reset the cooldown and resume hammering the API.

Also stores the last successful action so the userbot can keep a human-like
global gap between sends (independent of each group's interval).

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


def remaining(sender: str, min_gap: float = 0.0) -> float:
    """Seconds until this sender may act again (0 means ready now)."""
    try:
        state = _get(sender)
    except sqlite3.Error:
        log.warning("could not read sender_state for %s", sender, exc_info=True)
        return 0.0
    now = _now()
    wait = 0.0
    blocked = _parse(state.get("blocked_until"))
    if blocked is not None:
        wait = max(wait, (blocked - now).total_seconds())
    last = _parse(state.get("last_action_at"))
    if last is not None and min_gap > 0:
        wait = max(wait, (last + timedelta(seconds=float(min_gap)) - now).total_seconds())
    return max(0.0, wait)


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
