"""Ollama client: structured NLU (JSON) plus free-form reply generation.

The LLM never decides by free generation what to answer for the common cases —
the pipeline does (doc 01 §6). Here we only talk to Ollama and validate output.
"""

import json
import logging
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from cuadra import config
from cuadra.ai import prompts

log = logging.getLogger("cuadra.llm")


class LLMUnavailable(ConnectionError):
    """Ollama is down or unreachable (transient — the queue retries)."""


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


async def chat(
    messages: list[dict],
    *,
    json_format: bool = False,
    temperature: float | None = None,
    timeout: float | None = None,
) -> str:
    payload: dict = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "num_ctx": config.LLM_NUM_CTX,
        },
    }
    if json_format:
        payload["format"] = "json"
    try:
        async with httpx.AsyncClient(timeout=timeout or config.OLLAMA_TIMEOUT_S) as client:
            response = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 — everything here is transient/network
        raise LLMUnavailable(f"ollama request failed: {exc}") from exc
    return (data.get("message") or {}).get("content", "").strip()


async def is_available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{config.OLLAMA_URL}/api/tags")
            return response.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def classify(base_messages: list[dict], last_message: str) -> Intent:
    """NLU with one retry including the validation error (doc 03 §2)."""
    task = {
        "role": "user",
        "content": f"{prompts.NLU_TASK}\n\nMENSAJE DEL CLIENTE:\n{last_message}",
    }
    messages = [*base_messages, task]
    raw = await chat(messages, json_format=True, temperature=0.0)
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
        raw_retry = await chat(retry, json_format=True, temperature=0.0)
        try:
            return Intent.model_validate_json(raw_retry)
        except ValidationError as second_error:
            raise LLMInvalidOutput(str(second_error)) from second_error


async def compose(base_messages: list[dict], last_message: str, *, extra: str = "") -> str:
    instruction = prompts.COMPOSE_INSTRUCTION + (f"\n{extra}" if extra else "")
    messages = [
        *base_messages,
        {"role": "user", "content": f"{instruction}\n\nMENSAJE DEL CLIENTE:\n{last_message}"},
    ]
    return await chat(messages)


async def summarize(previous_summary: str | None, old_messages: list[dict]) -> str:
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
    )
    return raw.strip()


def estimate_tokens(messages: list[dict]) -> int:
    """Cheap ~4 chars/token estimate to keep the window under budget (01 §6.1)."""
    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return chars // 4
