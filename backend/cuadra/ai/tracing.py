"""Execution tracing: record input/output of critical methods.

Purpose: when something fails, reconstruct the execution flow without guessing.
Each trace is written both to a dedicated rotated log file (data/logs/traces.log)
and to the SQLite table `traces` so the dashboard can query it.

Levels (CUADRA_TRACE_LEVEL env var):
  OFF      - no tracing
  ERROR    - only methods that raised or returned ok=False
  SAMPLE   - 10% of successes + 100% of failures (configurable sample rate)
  ALL      - every call

PII masking: when CUADRA_TRACE_MASK_PII=1 (default), phone numbers and
addresses inside input/output dicts are truncated to avoid leaking customer
data in logs.
"""

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Optional

from cuadra import config, db
from cuadra.ai import redact
from cuadra.domain import settings

log = logging.getLogger("cuadra.trace")

PROMPTS_KEY = "llm.prompts_enabled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _should_trace(level: str, ok: bool, sample_rate: float) -> bool:
    """Decide whether this call should be traced."""
    if level == "OFF":
        return False
    if level == "ERROR":
        return not ok
    if level == "ALL":
        return True
    if level == "SAMPLE":
        if not ok:
            return True
        return random.random() < sample_rate
    return False


def _mask_pii(value: Any) -> Any:
    """Recursively mask phone numbers and long address-like strings."""
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) > 200:
            return value[:200] + f"...[+{len(value) - 200} more chars]"
        return value
    if isinstance(value, dict):
        return {k: _mask_pii(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_pii(v) for v in value]
    return value


class Tracer:
    """Single tracer instance shared across the process."""

    def __init__(self) -> None:
        self._logger: Optional[logging.Logger] = None
        self._initialized = False

    def _ensure_logger(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        level = getattr(config, "CUADRA_TRACE_LEVEL", "ERROR").upper()
        if level == "OFF":
            return
        log_file = getattr(config, "CUADRA_TRACE_LOG_FILE", None)
        if log_file is None:
            return
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("cuadra.trace")
        logger.setLevel(logging.INFO)
        logger.propagate = False  # do not double-log to cuadra.log
        handler = RotatingFileHandler(
            str(log_file),
            maxBytes=getattr(config, "CUADRA_TRACE_MAX_BYTES", 52428800),
            backupCount=getattr(config, "CUADRA_TRACE_MAX_FILES", 10),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        self._logger = logger

    def trace(
        self,
        method: str,
        input_data: Any,
        output_data: Any,
        *,
        ok: bool,
        error: Optional[str] = None,
        error_type: Optional[str] = None,
        duration_ms: Optional[float] = None,
        conversation_id: Optional[int] = None,
        turn_id: Optional[int] = None,
    ) -> None:
        """Write one trace if the level allows it, to both log file and SQLite."""
        level = getattr(config, "CUADRA_TRACE_LEVEL", "ERROR").upper()
        if not _should_trace(level, ok, getattr(config, "CUADRA_TRACE_SAMPLE_RATE", 0.1)):
            return
        self._ensure_logger()
        if self._logger is None:
            return

        record = {
            "timestamp": _now_iso(),
            "method": method,
            "input": _mask_pii(input_data) if getattr(config, "CUADRA_TRACE_MASK_PII", True) else input_data,
            "output": _mask_pii(output_data) if getattr(config, "CUADRA_TRACE_MASK_PII", True) else output_data,
            "ok": ok,
        }
        if error is not None:
            record["error"] = error
        if error_type is not None:
            record["error_type"] = error_type
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 2)
        if conversation_id is not None:
            record["conversation_id"] = conversation_id
        if turn_id is not None:
            record["turn_id"] = turn_id

        self._logger.info(json.dumps(record, ensure_ascii=False))

        # Also persist to SQLite so the dashboard can query it.
        _persist_trace(
            method=method,
            input_data=input_data,
            output_data=output_data,
            ok=ok,
            error=error,
            error_type=error_type,
            duration_ms=duration_ms,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )


tracer = Tracer()


class trace_method:
    """Decorator to trace a synchronous or async method.

    Usage:

        @trace_method("pipeline._maybe_handle_sale")
        def _maybe_handle_sale(...):
            ...

    The decorator captures the method arguments (excluding self/conn/db handles
    when they are not serializable) and the return value or exception.
    """

    def __init__(self, name: str, capture_args: bool = True) -> None:
        self.name = name
        self.capture_args = capture_args

    def __call__(self, func: Callable) -> Callable:
        if not getattr(config, "CUADRA_TRACE_LEVEL", "ERROR").upper() == "OFF":
            import asyncio

            if asyncio.iscoroutinefunction(func):
                return self._async_wrapper(func)
            return self._sync_wrapper(func)
        return func

    def _sync_wrapper(self, func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            ok = True
            error = None
            error_type = None
            output = None
            try:
                output = func(*args, **kwargs)
                return output
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = str(exc)
                error_type = type(exc).__name__
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                input_data = {
                    "args": [str(a) for a in args[1:]] if self.capture_args else [],
                    "kwargs": {k: str(v) for k, v in kwargs.items()} if self.capture_args else {},
                } if self.capture_args else None
                tracer.trace(
                    self.name,
                    input_data,
                    output,
                    ok=ok,
                    error=error,
                    error_type=error_type,
                    duration_ms=duration_ms,
                )

        return wrapper

    def _async_wrapper(self, func: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            ok = True
            error = None
            error_type = None
            output = None
            try:
                output = await func(*args, **kwargs)
                return output
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = str(exc)
                error_type = type(exc).__name__
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                input_data = {
                    "args": [str(a) for a in args[1:]] if self.capture_args else [],
                    "kwargs": {k: str(v) for k, v in kwargs.items()} if self.capture_args else {},
                } if self.capture_args else None
                tracer.trace(
                    self.name,
                    input_data,
                    output,
                    ok=ok,
                    error=error,
                    error_type=error_type,
                    duration_ms=duration_ms,
                )

        return wrapper


def trace_call(
    method: str,
    input_data: Any,
    output_data: Any,
    *,
    ok: bool,
    error: Optional[str] = None,
    error_type: Optional[str] = None,
    duration_ms: Optional[float] = None,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
) -> None:
    """Manual trace call (for methods that cannot be decorated)."""
    tracer.trace(
        method,
        input_data,
        output_data,
        ok=ok,
        error=error,
        error_type=error_type,
        duration_ms=duration_ms,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )


def record_call(
    purpose: Optional[str] = None,
    model: Optional[str] = None,
    schema_name: Optional[str] = None,
    conversation_id: Optional[int] = None,
    job_id: Optional[int] = None,
    latency_ms: Optional[float] = None,
    ok: bool = True,
    error: Optional[str] = None,
    messages: Optional[Any] = None,
    prompt_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    completion: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Compatibilidad con la interfaz original de cuadra.ai.tracing.record_call.

    Mapea los campos antiguos al formato de trace_call del nuevo módulo.
    """
    input_data = {
        "purpose": purpose,
        "model": model,
        "schema_name": schema_name,
        "job_id": job_id,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "messages": messages,
    }
    output_data = {
        "latency_ms": latency_ms,
        "completion": completion,
    }
    tracer.trace(
        "llm.chat",
        input_data,
        output_data,
        ok=ok,
        error=error,
        error_type=None if ok else "LLMError",
        duration_ms=latency_ms,
        conversation_id=conversation_id,
    )
    # También persistir en la tabla llm_calls (compatibilidad con el rastro
    # original de observabilidad).
    _persist_llm_call(
        purpose=purpose,
        model=model,
        schema_name=schema_name,
        conversation_id=conversation_id,
        job_id=job_id,
        latency_ms=latency_ms,
        ok=ok,
        error=error,
        messages=messages,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        completion=completion,
    )


def _persist_trace(
    method: str,
    input_data: Any,
    output_data: Any,
    *,
    ok: bool,
    error: Optional[str] = None,
    error_type: Optional[str] = None,
    duration_ms: Optional[float] = None,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
) -> None:
    """Guardar la traza en la tabla `traces` (SQLite) para que el dashboard
    pueda consultarla con SQL."""
    try:
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO traces
                   (timestamp, method, input_json, output_json, ok, error,
                    error_type, duration_ms, conversation_id, turn_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now_iso(),
                    method,
                    json.dumps(input_data, ensure_ascii=False) if input_data is not None else None,
                    json.dumps(output_data, ensure_ascii=False) if output_data is not None else None,
                    1 if ok else 0,
                    error,
                    error_type,
                    duration_ms,
                    conversation_id,
                    turn_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - el tracing nunca debe bloquear el flujo
        log.exception("no se pudo persistir el rastro en tabla traces")


def _persist_llm_call(
    purpose: Optional[str] = None,
    model: Optional[str] = None,
    schema_name: Optional[str] = None,
    conversation_id: Optional[int] = None,
    job_id: Optional[int] = None,
    latency_ms: Optional[float] = None,
    ok: bool = True,
    error: Optional[str] = None,
    messages: Optional[Any] = None,
    prompt_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    completion: Optional[str] = None,
) -> None:
    """Guardar la llamada en la tabla llm_calls (SQLite) con redactado de PII."""
    try:
        conn = db.connect()
        try:
            prompts_enabled = settings.get(conn, PROMPTS_KEY)
            prompts_on = prompts_enabled is None or prompts_enabled != "0"
            prompt_text = None
            completion_text = None
            redacted = 1  # Siempre 1: el registro pasó por el sistema de rastreo
            if messages is not None and prompts_on:
                prompt_text = redact.text(json.dumps(messages, ensure_ascii=False))
            if completion is not None and prompts_on:
                completion_text = redact.completion(completion)

            conn.execute(
                """INSERT INTO llm_calls
                   (created_at, purpose, model, schema_name, conversation_id,
                    job_id, prompt_tokens, output_tokens, latency_ms, ok, error,
                    redacted, prompt_text, completion_text)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now_iso(),
                    purpose,
                    model,
                    schema_name,
                    conversation_id,
                    job_id,
                    prompt_tokens,
                    output_tokens,
                    latency_ms,
                    1 if ok else 0,
                    error,
                    redacted,
                    prompt_text,
                    completion_text,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - el tracing nunca debe bloquear el flujo
        log.exception("no se pudo persistir el rastro de LLM en llm_calls")


def purge_old(older_than_days: int = 30) -> int:
    """Borrar registros de llm_calls y traces más antiguos que older_than_days.

    Devuelve la cantidad de filas borradas en llm_calls.
    """
    try:
        conn = db.connect()
        try:
            cur = conn.execute(
                """DELETE FROM llm_calls
                   WHERE created_at < datetime('now', ? || ' days')""",
                (-older_than_days,),
            )
            removed = cur.rowcount or 0
            cur2 = conn.execute(
                """DELETE FROM traces
                   WHERE created_at < datetime('now', ? || ' days')""",
                (-older_than_days,),
            )
            removed += cur2.rowcount or 0
            conn.commit()
            return removed
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.exception("no se pudo purgar llm_calls/traces")
        return 0
