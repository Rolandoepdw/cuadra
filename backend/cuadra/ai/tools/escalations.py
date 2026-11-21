"""Escalation reads (L3.4).

The bot asks the TCP for a detail the catalog does not have. These tools expose
the status of that question and the open questions of a conversation, so the
model can answer "already asked", "the supplier confirmed" or "still waiting"
without inventing an answer. Writes stay in `domain/escalations` and the TCP
loop.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cuadra.ai.tools.base import READ, Registry, ToolSpec, timeout_seconds
from cuadra.domain import escalations as escalations_domain


class StatusArgs(BaseModel):
    escalation_id: int | None = Field(default=None, ge=1)
    product_id: int | None = Field(default=None, ge=1)


class PendingArgs(BaseModel):
    conversation_id: int = Field(ge=1)


def _supplier_name(conn, supplier_id: int | None) -> str | None:
    if not supplier_id:
        return None
    row = conn.execute(
        "SELECT name FROM suppliers WHERE id = ?", (int(supplier_id),)
    ).fetchone()
    return str(row["name"]) if row is not None else None


def _summary(conn, escalation: dict) -> dict:
    return {
        "escalation_id": int(escalation["id"]),
        "status": escalation.get("status"),
        "question": escalation.get("question"),
        "answer": escalation.get("tcp_answer"),
        "product_id": escalation.get("product_id"),
        "supplier_id": escalation.get("supplier_id"),
        "supplier_name": _supplier_name(conn, escalation.get("supplier_id")),
        "created_at": escalation.get("created_at"),
        "answered_at": escalation.get("answered_at"),
        "resolved_at": escalation.get("resolved_at"),
    }


def status(
    conn,
    escalation_id: int | None = None,
    product_id: int | None = None,
) -> dict:
    escalation = None
    if escalation_id is not None:
        try:
            escalation = escalations_domain.get(conn, int(escalation_id))
        except escalations_domain.NotFound:
            escalation = None
    elif product_id is not None:
        escalation = escalations_domain.latest_for_product(conn, int(product_id))
    if escalation is None:
        return {"empty_reason": "no_escalation"}
    return {**_summary(conn, escalation), "empty_reason": None}


def pending(conn, conversation_id: int) -> dict:
    rows = escalations_domain.pending_for_conversation(
        conn, int(conversation_id)
    )
    items = [
        {
            "escalation_id": int(row["id"]),
            "status": row.get("status"),
            "question": row.get("question"),
            "product_id": row.get("product_id"),
            "supplier_id": row.get("supplier_id"),
            "created_at": row.get("created_at"),
            "answered_at": row.get("answered_at"),
        }
        for row in rows
    ]
    return {
        "conversation_id": int(conversation_id),
        "escalations": items,
        "empty_reason": None if items else "no_escalation",
    }


def register(registry: Registry) -> None:
    specs = (
        (
            "escalations.status",
            "Estado real de una consulta al TCP por id o producto. Úsala para «¿el proveedor respondió?».",
            StatusArgs,
            status,
        ),
        (
            "escalations.pending",
            "Consultas abiertas (pendientes o respondidas) de una conversación.",
            PendingArgs,
            pending,
        ),
    )
    for name, description, model, fn in specs:
        registry.register(
            ToolSpec(
                name=name,
                domain="escalations",
                owner="escalations",
                description=description,
                input_model=model,
                permission=READ,
                timeout_s=timeout_seconds(),
            ),
            fn,
        )
