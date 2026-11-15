"""LLM client: structured NLU (JSON) plus free-form reply generation.

Two transports, chosen by config.LLM_PROVIDER:

- ``openai``: any OpenAI-compatible ``/chat/completions`` endpoint. This is the
  default and points at Cuadra's own llama.cpp server (also works with LM Studio,
  vLLM, ...). Structured output uses ``response_format`` + ``json_schema``.
- ``ollama``: the native ``/api/chat`` endpoint, using Ollama's ``format`` field.

The LLM never decides by free generation what to answer for the common cases —
the pipeline does (doc 01 §6). Here we only talk to the model and validate output.
"""

import asyncio
import json
import logging
import time
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from cuadra import config, db
from cuadra.ai import prompts, tracing
from cuadra.domain import settings

log = logging.getLogger("cuadra.llm")

# H-24: short pause before retrying a compose whose TCP connection was reset.
COMPOSE_RETRY_DELAY_S = 1.0

# Serialize calls to the single-slot llama.cpp server (-np 1). Multiple
# concurrent queue workers would otherwise pile requests on the server socket.
_llm_semaphore = asyncio.Semaphore(1)


INTENTS = (
    "consulta_precio",
    "consulta_stock",
    "consulta_dato",
    "comparar",
    "comprar",
    "queja",
    "otro",
)


class LLMUnavailable(ConnectionError):
    """The model server is down or unreachable (transient — the queue retries)."""


class LLMInvalidOutput(Exception):
    """The model answered twice with invalid JSON for a structured task."""


class Intent(BaseModel):
    intencion: Literal[
        "consulta_precio",
        "consulta_stock",
        "consulta_dato",
        "comparar",
        "comprar",
        "queja",
        "otro",
    ]
    producto: str | None = None
    datos_faltantes: list[str] = Field(default_factory=list)
    confianza: float = Field(default=0.0, ge=0.0, le=1.0)
    cantidad: int | None = Field(default=None, ge=1)


INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intencion": {"type": "string", "enum": list(INTENTS)},
        "producto": {"type": ["string", "null"]},
        "datos_faltantes": {"type": "array", "items": {"type": "string"}},
        "confianza": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "cantidad": {"type": ["integer", "null"], "minimum": 1},
    },
    "required": ["intencion", "producto", "datos_faltantes", "confianza", "cantidad"],
    "additionalProperties": False,
}


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}


def _chat_url() -> str:
    if config.LLM_PROVIDER == "ollama":
        return f"{config.LLM_BASE_URL}/api/chat"
    return f"{config.LLM_BASE_URL}/chat/completions"


def _models_url() -> str:
    if config.LLM_PROVIDER == "ollama":
        return f"{config.LLM_BASE_URL}/api/tags"
    return f"{config.LLM_BASE_URL}/models"


def _extract_content(data: dict) -> str:
    if config.LLM_PROVIDER == "ollama":
        return (data.get("message") or {}).get("content", "")
    choices = data.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content", "")


def _usage(data: dict) -> tuple[int | None, int | None]:
    """Token counts from either transport, when the backend reports them."""
    usage = data.get("usage") or {}
    prompt = usage.get("prompt_tokens", data.get("prompt_eval_count"))
    output = usage.get("completion_tokens", data.get("eval_count"))
    return (
        prompt if isinstance(prompt, int) else None,
        output if isinstance(output, int) else None,
    )


async def _post_chat(client: httpx.AsyncClient, payload: dict, structured: bool) -> dict:
    async with _llm_semaphore:
        response = await client.post(_chat_url(), json=payload, headers=_headers())
        if response.status_code == 400 and structured:
            # Some endpoints reject response_format/format: retry as plain text.
            payload = {k: v for k, v in payload.items() if k not in ("response_format", "format")}
            response = await client.post(_chat_url(), json=payload, headers=_headers())
        response.raise_for_status()
        return response.json()


def _active_model() -> str:
    """Model selected by the operator (settings), else the config default."""
    conn = db.connect()
    try:
        return settings.get(conn, settings.LLM_MODEL_KEY) or config.LLM_MODEL
    finally:
        conn.close()


async def chat(
    messages: list[dict],
    *,
    schema: dict | None = None,
    json_object: bool = False,
    temperature: float | None = None,
    timeout: float | None = None,
    purpose: str = "other",
    schema_name: str | None = None,
    conversation_id: int | None = None,
    job_id: int | None = None,
) -> str:
    """Single completion. ``schema`` (OpenAI-style JSON Schema) or ``json_object``
    request structured output where the backend supports it.

    Every call is persisted to ``llm_calls`` (redacted) so phase 0 can measure
    the agent. ``purpose`` labels the call in that table.
    """
    temp = config.LLM_TEMPERATURE if temperature is None else temperature
    model = _active_model()
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temp,
    }
    structured = False
    if config.LLM_PROVIDER == "ollama":
        payload["options"] = {"temperature": temp, "num_ctx": config.LLM_CTX}
        if schema is not None:
            payload["format"] = schema
            structured = True
        elif json_object:
            payload["format"] = "json"
            structured = True
    else:
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name or "output",
                    "strict": True,
                    "schema": schema,
                },
            }
            structured = True
        elif json_object:
            payload["response_format"] = {"type": "json_object"}
            structured = True

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout or config.LLM_TIMEOUT_S) as client:
            data = await _post_chat(client, payload, structured)
    except Exception as exc:  # noqa: BLE001 — everything here is transient/network
        latency_ms = int((time.perf_counter() - started) * 1000)
        tracing.record_call(
            purpose=purpose,
            model=model,
            schema_name=schema_name,
            conversation_id=conversation_id,
            job_id=job_id,
            latency_ms=latency_ms,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            messages=messages,
        )
        raise LLMUnavailable(f"llm request failed ({config.LLM_PROVIDER}): {exc}") from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    content = _extract_content(data).strip()
    prompt_tokens, output_tokens = _usage(data)
    tracing.record_call(
        purpose=purpose,
        model=model,
        schema_name=schema_name,
        conversation_id=conversation_id,
        job_id=job_id,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        ok=True,
        messages=messages,
        completion=content,
    )
    return content


async def health() -> dict:
    """Probe the endpoint. Returns {'status': 'ok'|'unreachable'|'error_NNN'}."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(_models_url(), headers=_headers())
        if response.status_code == 200:
            return {"status": "ok"}
        return {"status": f"error_{response.status_code}"}
    except Exception:  # noqa: BLE001
        return {"status": "unreachable"}


async def is_available() -> bool:
    return (await health())["status"] == "ok"


async def classify(
    base_messages: list[dict],
    last_message: str,
    *,
    conversation_id: int | None = None,
) -> Intent:
    """NLU with one retry including the validation error (doc 03 §2)."""
    task = {
        "role": "user",
        "content": f"{prompts.NLU_TASK}\n\nMENSAJE DEL CLIENTE:\n{last_message}",
    }
    messages = [*base_messages, task]
    raw = await chat(
        messages,
        schema=INTENT_SCHEMA,
        schema_name="intent",
        temperature=0.0,
        purpose="intent",
        conversation_id=conversation_id,
    )
    try:
        return Intent.model_validate_json(raw)
    except ValidationError as first_error:
        log.warning("NLU output invalid, retrying once: %s", first_error)
        correction = {
            "role": "user",
            "content": (
                "Tu respuesta anterior no validó contra el esquema JSON. "
                f"Error: {first_error.errors(include_url=False)}. "
                "Responde de nuevo SOLO con el JSON correcto."
            ),
        }
        retry = [*messages, {"role": "assistant", "content": raw}, correction]
        raw_retry = await chat(
            retry,
            schema=INTENT_SCHEMA,
            schema_name="intent",
            temperature=0.0,
            purpose="intent",
            conversation_id=conversation_id,
        )
        try:
            return Intent.model_validate_json(raw_retry)
        except ValidationError as second_error:
            raise LLMInvalidOutput(str(second_error)) from second_error


async def classify_light(
    base_messages: list[dict],
    text: str,
    *,
    conversation_id: int | None = None,
) -> Intent:
    """Lightweight NLU: minimal prompt for intent classification only.

    Uses NLU_TASK_LIGHT (~600 tokens) instead of the full system prompt
    (~2171 tokens). Intended for turns where we only need to classify, not
    compose a free-form reply.
    """
    task = {
        "role": "user",
        "content": prompts.NLU_TASK_LIGHT.format(text=text),
    }
    messages = [*base_messages, task]
    raw = await chat(
        messages,
        schema=INTENT_SCHEMA,
        schema_name="intent",
        temperature=0.0,
        purpose="intent",
        conversation_id=conversation_id,
    )
    try:
        return Intent.model_validate_json(raw)
    except ValidationError as first_error:
        log.warning("NLU light output invalid, retrying once: %s", first_error)
        correction = {
            "role": "user",
            "content": (
                "Tu respuesta anterior no validó contra el esquema JSON. "
                f"Error: {first_error.errors(include_url=False)}. "
                "Responde de nuevo SOLO con el JSON correcto."
            ),
        }
        retry = [*messages, {"role": "assistant", "content": raw}, correction]
        raw_retry = await chat(
            retry,
            schema=INTENT_SCHEMA,
            schema_name="intent",
            temperature=0.0,
            purpose="intent",
            conversation_id=conversation_id,
        )
        try:
            return Intent.model_validate_json(raw_retry)
        except ValidationError as second_error:
            raise LLMInvalidOutput(str(second_error)) from second_error


async def compose(
    base_messages: list[dict],
    last_message: str,
    *,
    extra: str = "",
    conversation_id: int | None = None,
) -> str:
    instruction = prompts.COMPOSE_INSTRUCTION + (f"\n{extra}" if extra else "")
    messages = [
        *base_messages,
        {"role": "user", "content": f"{instruction}\n\nMENSAJE DEL CLIENTE:\n{last_message}"},
    ]
    try:
        return await chat(messages, purpose="compose", conversation_id=conversation_id)
    except LLMUnavailable as exc:
        # H-24: after a hard restart the server may reset the connection of the
        # request orphaned by the dead core. One short retry avoids paying the
        # job queue's long backoff for a transport hiccup.
        if not isinstance(exc.__cause__, httpx.TransportError):
            raise
        log.warning("compose transport error (%s); retrying once", exc.__cause__)
        await asyncio.sleep(COMPOSE_RETRY_DELAY_S)
        return await chat(messages, purpose="compose", conversation_id=conversation_id)


async def summarize(
    previous_summary: str | None,
    old_messages: list[dict],
    *,
    conversation_id: int | None = None,
) -> str:
    def line(message: dict) -> str:
        speaker = "Cliente" if message["direction"] == "INBOUND" else "Vendedor"
        body = message["body"] or "[" + message["type"] + "]"
        return f"{speaker}: {body}"

    transcript = "\n".join(line(m) for m in old_messages)
    parts = [prompts.SUMMARY_INSTRUCTION]
    if previous_summary:
        parts.append(f"RESUMEN ANTERIOR:\n{previous_summary}")
    parts.append(f"CONVERSACIÓN:\n{transcript}")
    raw = await chat(
        [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        temperature=0.2,
        purpose="summary",
        conversation_id=conversation_id,
    )
    return raw.strip()


def estimate_tokens(messages: list[dict]) -> int:
    """Cheap ~3.5 chars/token estimate for Spanish (doc 01 §5)."""
    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return int(chars / 3.5)
