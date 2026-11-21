"""Closed registry of read-only tools for the agent.

The model may propose a tool call, but only a name registered here can run, and
only if the current mode allows it. Writes never go through this registry: money
and stock invariants live in the domain state machine.

Contract (doc 14 §5):

- `ToolResult.ok=True` with empty data is a real answer: `empty_reason` explains
  why ("no_match", "category_empty", "no_stock").
- `ok=False` is an execution failure: `invalid_args`, `not_found`, `ambiguous`,
  `unavailable`, `timeout` or `forbidden`.
- The cache lives in the `TurnFrame`; it is not a latency budget, it is
  correctness (two reads of the same product in one turn must agree).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from cuadra import config
from cuadra.ai import redact, tracing

ToolMode = Literal["off", "shadow", "on"]

READ = "read"
WRITE = "write"

# Stable error codes. A caller can branch on these without parsing messages.
INVALID_ARGS = "invalid_args"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"
UNAVAILABLE = "unavailable"
TIMEOUT = "timeout"
FORBIDDEN = "forbidden"


class ToolError(Exception):
    """A tool failure with a stable code (doc 14 §5.3)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class ToolSpec:
    """Everything the planner and the validator need to know about a tool."""

    name: str
    domain: str
    description: str
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None
    permission: Literal["read", "write"] = READ
    owner: str = ""
    cache_ttl_s: int = 0
    timeout_s: float = 5.0
    redact: tuple[str, ...] = ()
    mode_key: str | None = None


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    source: str = ""
    as_of: str = ""
    confidence: float = 1.0
    evidence: list[dict] = field(default_factory=list)
    empty_reason: str | None = None
    error: ToolError | None = None
    cache_hit: bool = False
    duration_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "data": self.data,
            "source": self.source,
            "as_of": self.as_of,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "empty_reason": self.empty_reason,
            "cache_hit": self.cache_hit,
        }
        if self.error is not None:
            payload["error"] = self.error.as_dict()
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        return payload


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any) -> Any:
    """Mask PII in a tool payload before it reaches the trace (D7)."""
    if isinstance(value, str):
        return redact.text(value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _cache_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


class Registry:
    """A closed list of tools: unknown names never reach the domain."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._fns: dict[str, Callable[..., Any]] = {}

    def register(self, spec: ToolSpec, fn: Callable[..., Any]) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name}")
        self._specs[spec.name] = spec
        self._fns[spec.name] = fn

    def has(self, name: str) -> bool:
        return name in self._specs

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolError(FORBIDDEN, f"unknown tool: {name}") from exc

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def allowed(self, mode: str) -> list[ToolSpec]:
        """Tools the model may see in this mode.

        `off` hides everything: the deterministic route keeps deciding.
        `shadow` and `on` expose reads; writes are never exposed.
        Per-domain mode keys are resolved by the caller in L3.1.
        """
        if mode not in ("shadow", "on"):
            return []
        return [spec for spec in self._specs.values() if spec.permission == READ]

    def run(self, name: str, args: dict[str, Any] | None, frame) -> ToolResult:
        args = dict(args or {})
        started = time.perf_counter()
        try:
            spec = self.get(name)
        except ToolError as exc:
            return self._fail(spec=None, name=name, error=exc, started=started, frame=frame)

        if spec.permission != READ:
            return self._fail(
                spec=spec,
                name=name,
                error=ToolError(FORBIDDEN, f"write tool is not callable: {name}"),
                started=started,
                frame=frame,
            )

        validated = args
        if spec.input_model is not None:
            try:
                validated = spec.input_model.model_validate(args).model_dump()
            except ValidationError as exc:
                return self._fail(
                    spec=spec,
                    name=name,
                    error=ToolError(INVALID_ARGS, str(exc), {"errors": exc.errors()}),
                    started=started,
                    frame=frame,
                )

        key = _cache_key(name, validated)
        cached = getattr(frame, "cache", {}).get(key)
        if cached is not None:
            return replace(cached, cache_hit=True)

        try:
            data = self._fns[name](getattr(frame, "conn", None), **validated)
        except ToolError as exc:
            return self._fail(spec=spec, name=name, error=exc, started=started, frame=frame)
        except Exception as exc:  # noqa: BLE001 - the caller gets a stable code
            return self._fail(
                spec=spec,
                name=name,
                error=ToolError(UNAVAILABLE, str(exc), {"type": type(exc).__name__}),
                started=started,
                frame=frame,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        if spec.timeout_s and duration_ms > spec.timeout_s * 1000:
            return self._fail(
                spec=spec,
                name=name,
                error=ToolError(
                    TIMEOUT,
                    f"{name} took {duration_ms:.0f} ms (limit {spec.timeout_s:.0f} s)",
                    {"duration_ms": round(duration_ms, 2)},
                ),
                started=started,
                frame=frame,
            )

        result = self._result(spec, data)
        result.duration_ms = round(duration_ms, 2)
        getattr(frame, "cache", {})[key] = result
        self._trace(frame, name, validated, result)
        return result

    # ------------------------------------------------------------------ helpers
    def _result(self, spec: ToolSpec, data: Any) -> ToolResult:
        source = f"{spec.owner or spec.domain}.{spec.name.split('.')[-1]}"
        evidence: list[dict] = []
        empty_reason: str | None = None
        if isinstance(data, dict) and "empty_reason" in data:
            empty_reason = data.get("empty_reason")
        if isinstance(data, list):
            evidence = [
                {"id": row.get("id")} for row in data if isinstance(row, dict) and row.get("id")
            ]
            if not data:
                empty_reason = empty_reason or "no_match"
        elif isinstance(data, dict) and isinstance(data.get("products"), list):
            evidence = [
                {
                    "id": row.get("id"),
                    "availability": row.get("availability"),
                    "stock": row.get("stock"),
                }
                for row in data["products"]
                if isinstance(row, dict) and row.get("id")
            ]
            if not data["products"]:
                empty_reason = empty_reason or data.get("empty_reason") or "no_match"
        elif isinstance(data, dict) and isinstance(data.get("sales"), list):
            evidence = [
                {"sale_id": row.get("sale_id"), "status": row.get("status")}
                for row in data["sales"]
                if isinstance(row, dict) and row.get("sale_id")
            ]
            if not data["sales"]:
                empty_reason = empty_reason or data.get("empty_reason") or "no_sale"
        elif isinstance(data, dict) and isinstance(data.get("facts"), list):
            evidence = [
                {"fact_key": row.get("key")}
                for row in data["facts"]
                if isinstance(row, dict) and row.get("key")
            ]
            if not data["facts"]:
                empty_reason = empty_reason or data.get("empty_reason") or "no_facts"
        elif isinstance(data, dict) and isinstance(data.get("profile"), dict):
            evidence = [{"profile": True}]
            if not data["profile"]:
                empty_reason = (
                    empty_reason or data.get("empty_reason") or "no_profile"
                )
        elif isinstance(data, dict) and "summary" in data:
            evidence = [{"summary": bool(data.get("summary"))}]
        elif isinstance(data, dict) and data.get("sale_id"):
            evidence = [{"sale_id": data["sale_id"], "status": data.get("status")}]
        elif isinstance(data, dict) and data.get("zone_id"):
            evidence = [{"zone_id": data["zone_id"], "covered": data.get("covered")}]
        elif isinstance(data, dict) and "eta" in data:
            evidence = [{"eta": data.get("eta"), "source": data.get("source")}]
        elif isinstance(data, dict) and data.get("product_id"):
            evidence = [{"product_id": data["product_id"]}]
        elif isinstance(data, dict) and data.get("id"):
            evidence = [{"id": data["id"]}]
        return ToolResult(
            ok=True,
            data=data,
            source=source,
            as_of=_now_iso(),
            evidence=evidence,
            empty_reason=empty_reason,
        )

    def _fail(self, *, spec, name, error, started, frame) -> ToolResult:
        result = ToolResult(
            ok=False,
            data=None,
            source=f"{spec.owner or spec.domain}.{name.split('.')[-1]}" if spec else name,
            as_of=_now_iso(),
            error=error,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        self._trace(frame, name, {}, result)
        return result

    def _trace(self, frame, name: str, args: dict[str, Any], result: ToolResult) -> None:
        conversation = getattr(frame, "conversation", None) or {}
        tracing.trace_call(
            f"tool.{name}",
            _redact({"args": args}),
            _redact(result.as_dict()),
            ok=result.ok,
            error=result.error.message if result.error else None,
            error_type=result.error.code if result.error else None,
            duration_ms=result.duration_ms,
            conversation_id=conversation.get("id"),
            turn_id=getattr(frame, "turn_id", None),
        )

    def invalidate(self, frame) -> None:
        """Drop per-turn reads after a write (the state machine changed facts)."""
        getattr(frame, "cache", {}).clear()


def timeout_seconds() -> float:
    return float(getattr(config, "AGENT_TOOL_TIMEOUT_S", 5.0))


registry = Registry()


def register(spec: ToolSpec, fn: Callable[..., Any]) -> None:
    registry.register(spec, fn)


def run(name: str, args: dict[str, Any] | None, frame) -> ToolResult:
    return registry.run(name, args, frame)
