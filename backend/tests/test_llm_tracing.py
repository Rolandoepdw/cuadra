"""Phase 0: redacted LLM traces and their retention.

These tests run against a throwaway SQLite database (never the real
`backend/data/cuadra.db`) and never call a model server.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from cuadra import config, db
from cuadra.ai import llm, redact, tracing


class RedactTests(unittest.TestCase):
    def test_masks_cuban_phone_numbers(self):
        for raw in (
            "mi telefono es 55551234",
            "llamame al +53 5555 1234",
            "whatsapp 5355551234",
        ):
            masked = redact.text(raw)
            self.assertIn(redact.PHONE, masked)
            self.assertNotIn("5555", masked)

    def test_masks_short_numbers_only_near_phone_keywords(self):
        self.assertIn(redact.PHONE, redact.text("mi celular 5555555"))
        # A bare 6-digit number is a CUP price, not a phone.
        self.assertEqual(redact.text("cuesta 125000 cup"), "cuesta 125000 cup")

    def test_keeps_prices_and_small_numbers(self):
        self.assertEqual(redact.text("precio 12.50 usd"), "precio 12.50 usd")
        self.assertEqual(redact.text("stock 3 y 10000 mah"), "stock 3 y 10000 mah")

    def test_masks_addresses_and_emails(self):
        masked = redact.text("vivo en calle 23 #45, apto 6. correo ana@example.com")
        self.assertIn(redact.ADDRESS, masked)
        self.assertIn(redact.EMAIL, masked)
        self.assertNotIn("ana@", masked)

    def test_messages_serializes_redacted_content(self):
        payload = redact.messages(
            [
                {"role": "system", "content": "producto real"},
                {"role": "user", "content": "mi tel es 55551234"},
            ]
        )
        parsed = json.loads(payload)
        self.assertEqual(parsed[0]["content"], "producto real")
        self.assertIn(redact.PHONE, parsed[1]["content"])

    def test_truncates_long_traces(self):
        value = redact.completion("x" * (redact.TRACE_MAX_CHARS + 50))
        self.assertLessEqual(len(value), redact.TRACE_MAX_CHARS + 30)
        self.assertTrue(value.endswith("[truncated]"))


class TraceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute("INSERT INTO customers (name) VALUES ('Cliente de prueba')")
            conn.execute(
                "INSERT INTO conversations (customer_id, channel) VALUES (1, 'telegram')"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _rows(self) -> list:
        conn = db.connect()
        try:
            return conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_migration_creates_observability_objects(self):
        conn = db.connect()
        try:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(llm_calls)")
            }
            self.assertIn("prompt_text", columns)
            self.assertIn("redacted", columns)
            message_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(messages)")
            }
            self.assertIn("external_id", message_columns)
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("turns", tables)
        finally:
            conn.close()

    def test_record_call_stores_redacted_text(self):
        tracing.record_call(
            purpose="compose",
            model="test-model",
            conversation_id=1,
            prompt_tokens=10,
            output_tokens=3,
            latency_ms=250,
            messages=[{"role": "user", "content": "mi tel 55551234"}],
            completion="te llamo al 55551234",
        )
        row = self._rows()[0]
        self.assertEqual(row["purpose"], "compose")
        self.assertEqual(row["model"], "test-model")
        self.assertEqual((row["prompt_tokens"], row["output_tokens"]), (10, 3))
        self.assertEqual(row["redacted"], 1)
        self.assertIn(redact.PHONE, row["prompt_text"])
        self.assertIn(redact.PHONE, row["completion_text"])
        self.assertNotIn("55551234", row["prompt_text"])

    def test_prompts_can_be_disabled(self):
        conn = db.connect()
        try:
            from cuadra.domain import settings

            settings.set(conn, tracing.PROMPTS_KEY, "0")
        finally:
            conn.close()
        tracing.record_call(
            purpose="intent",
            model="test-model",
            messages=[{"role": "user", "content": "hola"}],
            completion="hola",
        )
        row = self._rows()[0]
        self.assertIsNone(row["prompt_text"])
        self.assertIsNone(row["completion_text"])
        self.assertEqual(row["redacted"], 1)

    def test_purge_removes_only_expired_traces(self):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO llm_calls (purpose, model, created_at)
                   VALUES ('intent', 'old', datetime('now', '-40 days'))"""
            )
            conn.commit()
        finally:
            conn.close()
        tracing.record_call(purpose="intent", model="new")

        removed = tracing.purge_old()

        self.assertEqual(removed, 1)
        models = [row["model"] for row in self._rows()]
        self.assertEqual(models, ["new"])

    def test_chat_records_usage_and_latency(self):
        response = {
            "choices": [{"message": {"content": "respuesta"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }
        with patch(
            "cuadra.ai.llm._post_chat", new=AsyncMock(return_value=response)
        ):
            reply = asyncio.run(
                llm.chat(
                    [{"role": "user", "content": "hola"}],
                    purpose="compose",
                    conversation_id=1,
                )
            )

        self.assertEqual(reply, "respuesta")
        row = self._rows()[0]
        self.assertEqual(row["ok"], 1)
        self.assertEqual((row["prompt_tokens"], row["output_tokens"]), (12, 4))
        self.assertIsNotNone(row["latency_ms"])
        self.assertEqual(row["completion_text"], "respuesta")


class ComposeRetryTests(unittest.TestCase):
    """H-24: one short retry when the model server resets the connection."""

    def _unavailable(self, cause: BaseException) -> llm.LLMUnavailable:
        try:
            raise llm.LLMUnavailable("llm request failed") from cause
        except llm.LLMUnavailable as error:
            return error

    def test_transport_error_is_retried_once(self):
        chat = AsyncMock(
            side_effect=[self._unavailable(httpx.ReadError("reset")), "respuesta"]
        )
        with (
            patch.object(llm, "chat", chat),
            patch.object(llm, "COMPOSE_RETRY_DELAY_S", 0),
        ):
            reply = asyncio.run(llm.compose([], "hola"))

        self.assertEqual(reply, "respuesta")
        self.assertEqual(chat.await_count, 2)

    def test_transport_error_is_not_retried_forever(self):
        chat = AsyncMock(
            side_effect=[
                self._unavailable(httpx.ReadError("reset")),
                self._unavailable(httpx.ReadError("reset again")),
            ]
        )
        with (
            patch.object(llm, "chat", chat),
            patch.object(llm, "COMPOSE_RETRY_DELAY_S", 0),
        ):
            with self.assertRaises(llm.LLMUnavailable):
                asyncio.run(llm.compose([], "hola"))

        self.assertEqual(chat.await_count, 2)

    def test_non_transport_errors_are_not_retried(self):
        chat = AsyncMock(side_effect=self._unavailable(ValueError("bad request")))
        with (
            patch.object(llm, "chat", chat),
            patch.object(llm, "COMPOSE_RETRY_DELAY_S", 0),
        ):
            with self.assertRaises(llm.LLMUnavailable):
                asyncio.run(llm.compose([], "hola"))

        self.assertEqual(chat.await_count, 1)


if __name__ == "__main__":
    unittest.main()
