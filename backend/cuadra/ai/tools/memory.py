"""Memory and customer reads (L3.3).

Facts are deterministic Python extractions; the profile is the customer layer
that survives a conversation closing; the summary is the rolling compression.
These tools expose those reads with a stable shape and an `empty_reason`. They
never write: `memory.capture_facts` and `memory.maybe_summarize` remain the only
writers, and the tracing layer masks phones and addresses.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cuadra.ai import memory
from cuadra.ai.tools.base import READ, Registry, ToolSpec, timeout_seconds
from cuadra.domain import conversations


class FactsArgs(BaseModel):
    conversation_id: int = Field(ge=1)


class ProfileArgs(BaseModel):
    customer_id: int = Field(ge=1)


class SummaryArgs(BaseModel):
    conversation_id: int = Field(ge=1)


def facts(conn, conversation_id: int) -> dict:
    rows = memory.list_facts(conn, int(conversation_id))
    items = [
        {
            "key": row.get("fact_key"),
            "value": row.get("fact_value"),
            "confidence": row.get("confidence"),
            "updated_at": row.get("updated_at"),
        }
        for row in rows
        if row.get("fact_key")
    ]
    return {
        "conversation_id": int(conversation_id),
        "facts": items,
        "empty_reason": None if items else "no_facts",
    }


def profile(conn, customer_id: int) -> dict:
    data = memory.customer_profile(conn, int(customer_id))
    return {
        "customer_id": int(customer_id),
        "profile": data,
        "empty_reason": None if data else "no_profile",
    }


def summary(conn, conversation_id: int) -> dict:
    context = conversations.get_context(conn, int(conversation_id))
    text = str(context.get("summary") or "").strip()
    return {
        "conversation_id": int(conversation_id),
        "summary": text or None,
        "through_message_id": context.get("summary_through_message_id"),
        "empty_reason": None if text else "no_summary",
    }


def register(registry: Registry) -> None:
    specs = (
        (
            "memory.facts",
            "Hechos vigentes de la conversación (nombre, zona, dirección, etc.).",
            FactsArgs,
            facts,
        ),
        (
            "memory.profile",
            "Perfil del cliente y sus últimas compras.",
            ProfileArgs,
            profile,
        ),
        (
            "memory.summary",
            "Resumen incremental de la conversación.",
            SummaryArgs,
            summary,
        ),
    )
    for name, description, model, fn in specs:
        registry.register(
            ToolSpec(
                name=name,
                domain="memory",
                owner="memory",
                description=description,
                input_model=model,
                permission=READ,
                timeout_s=timeout_seconds(),
            ),
            fn,
        )
