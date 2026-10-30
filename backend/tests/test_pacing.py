"""Pacing governor: local gap vs. real Telegram rate limits.

These tests run against a throwaway SQLite database (never the real
`backend/data/cuadra.db`) and never touch Telegram.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cuadra import config, db
from cuadra.ai import queue
from cuadra.channels import rate_limit
from cuadra.scheduler import broadcasts as scheduler


class PacingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        self._old_gap = config.USERBOT_MIN_SEND_GAP_S
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.USERBOT_MIN_SEND_GAP_S = 8.0
        conn = db.connect()
        try:
            db.apply_migrations(conn)
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        config.USERBOT_MIN_SEND_GAP_S = self._old_gap
        self._tmp.cleanup()

    # ------------------------------------------------------------------ helpers

    def _job(self, job_id: int) -> dict:
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def _count_events(self, event_type: str, job_id: int) -> int:
        conn = db.connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE type = ? AND entity_id = ?",
                (event_type, job_id),
            ).fetchone()[0]
        finally:
            conn.close()

    def _process(self, job_type: str, handler, payload: dict | None = None) -> int:
        queue.register_handler(job_type, handler)
        job_id = queue.enqueue(job_type, payload or {})
        conn = db.connect()
        try:
            job = queue._claim(conn)
        finally:
            conn.close()
        self.assertIsNotNone(job)
        asyncio.run(queue._process(job))
        return job_id


class LocalGapTests(PacingTestCase):
    def test_local_gap_parks_without_consuming_budget(self):
        """Cuadra's own pacing must not look like a Telegram punishment."""

        async def handler(_payload):
            raise queue.RateLimited(
                8,
                "userbot send gap: wait 8s",
                account=False,
                penalize=False,
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=8),
            )

        job_id = self._process("test_local_gap", handler)
        row = self._job(job_id)

        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["rate_limit_hits"], 0)
        self.assertEqual(row["attempts"], 0)
        self.assertIsNotNone(row["next_retry_at"])
        self.assertEqual(self._count_events("job_rate_limited", job_id), 0)

    def test_real_floodwait_consumes_hits_and_logs_event(self):
        async def handler(_payload):
            raise queue.RateLimited(30, "telegram rate limit: wait 30s")

        job_id = self._process("test_floodwait", handler)
        row = self._job(job_id)

        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["rate_limit_hits"], 1)
        self.assertEqual(row["attempts"], 0)
        self.assertIsNotNone(row["next_retry_at"])
        self.assertEqual(self._count_events("job_rate_limited", job_id), 1)


class ClaimGateTests(PacingTestCase):
    def test_claim_skips_broadcast_while_sender_is_busy(self):
        queue.register_handler("test_customer", lambda _p: None)
        queue.enqueue("send_broadcast", {"post_id": 1}, priority=config.PRIORITY_BROADCAST)
        queue.enqueue("test_customer", {}, priority=config.PRIORITY_CUSTOMER)

        conn = db.connect()
        try:
            first = queue._claim(conn, allow_broadcast=False)
            second = queue._claim(conn, allow_broadcast=True)
        finally:
            conn.close()

        self.assertEqual(first["type"], "test_customer")
        self.assertEqual(second["type"], "send_broadcast")


class SchedulerGateTests(PacingTestCase):
    def test_gate_blocks_while_gap_is_active(self):
        rate_limit.mark_action("userbot")
        conn = db.connect()
        try:
            allowed, reason = scheduler._admission_open(conn)
        finally:
            conn.close()

        self.assertFalse(allowed)
        self.assertIn("userbot busy", reason)

    def test_gate_blocks_with_one_broadcast_in_flight(self):
        queue.enqueue("send_broadcast", {"post_id": 1})
        conn = db.connect()
        try:
            allowed, reason = scheduler._admission_open(conn)
        finally:
            conn.close()

        self.assertFalse(allowed)
        self.assertIn("in flight", reason)

    def test_gate_allows_idle_sender(self):
        conn = db.connect()
        try:
            allowed, reason = scheduler._admission_open(conn)
        finally:
            conn.close()

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_has_pending_ignores_finished_jobs(self):
        conn = db.connect()
        try:
            job_id = queue.enqueue("send_broadcast", {"post_id": 2})
            queue._finish(conn, job_id, "COMPLETED")
            self.assertFalse(queue.has_pending("send_broadcast", conn))
        finally:
            conn.close()


class SnapshotTests(PacingTestCase):
    def test_snapshot_distinguishes_ready_gap_and_cooldown(self):
        self.assertEqual(rate_limit.snapshot("userbot", 8)["reason"], "ready")

        rate_limit.mark_action("userbot")
        gap = rate_limit.snapshot("userbot", 8)
        self.assertEqual(gap["reason"], "gap")
        self.assertGreater(gap["wait_s"], 0)
        self.assertLessEqual(gap["wait_s"], 8)

        rate_limit.set_cooldown("userbot", 120, "flood")
        cooldown = rate_limit.snapshot("userbot", 8)
        self.assertEqual(cooldown["reason"], "cooldown")
        self.assertGreater(cooldown["wait_s"], 8)
        self.assertIsNotNone(cooldown["blocked_until"])

    def test_gap_follows_configured_value(self):
        """Changing 8 -> 4 seconds changes the shared governor, not callers."""
        rate_limit.mark_action("userbot")

        self.assertGreater(rate_limit.local_gap_remaining("userbot", 8), 6)
        self.assertLess(rate_limit.local_gap_remaining("userbot", 4), 5)
        self.assertEqual(
            rate_limit.local_gap_remaining("userbot", 0), 0
        )


if __name__ == "__main__":
    unittest.main()
