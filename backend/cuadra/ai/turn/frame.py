"""TurnFrame: per-turn state and context persistence."""

import logging
import time
from dataclasses import dataclass, field

from cuadra.ai import memory
from cuadra.domain import conversations, sales as sales_domain

log = logging.getLogger("cuadra.frame")


def _update_context(
    conn,
    conversation,
    intent,
    product,
    generator: str,
    escalate: bool,
    *,
    stage: str | None = None,
    sale: dict | None = None,
    sale_qty: int | None = None,
    context: dict | None = None,
    clear_product: bool = False,
) -> None:
    # F5 (prueba #2): guardar el mismo dict que mutó el turno. Recargarlo desde
    # la BD borraba `last_template_*` y las variantes nunca rotaban.
    if context is None:
        context = conversations.get_context(conn, conversation["id"])
    context["conversation_id"] = conversation["id"]
    if clear_product:
        # Prueba #8: un listado de catálogo no deja un producto "en juego". Se
        # conservan sale_id/order_code para las preguntas post-venta.
        context.pop("product_id", None)
    elif product is not None:
        context["product_id"] = product["id"]
    context["last_intent"] = intent.intencion
    context["last_generator"] = generator
    context["pending_escalation"] = bool(escalate)
    if stage:
        context["stage"] = stage
    if sale is not None:
        context["sale_id"] = sale["id"]
        context["order_code"] = sale["order_code"]
        # Una venta nueva cierra el bucle de 'otro' y de quejas.
        context["otro_count"] = 0
        context["queja_count"] = 0
        # La cantidad acordada ya vive en la venta: no debe reaparecer en otra.
        context.pop("sale_qty", None)
        context.pop("sale_qty_product_id", None)
    elif sale_qty is not None:
        context["sale_qty"] = int(sale_qty)
        context["sale_qty_product_id"] = product["id"] if product is not None else None
    conversations.set_context(conn, conversation["id"], context)


# ------------------------------------------------------------------ TurnFrame
@dataclass
class Listing:
    """The last list of products shown to the customer.

    It lives in `context_json`, so "esos", "el segundo" and "el de antes" survive
    a restart. `product_ids` is the source of truth for ordinals; `labels` is
    only for telemetry and prompts.
    """

    product_ids: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    query: str = ""
    kind: str = ""
    created_at: str = ""

    def as_dict(self) -> dict:
        return {
            "product_ids": list(self.product_ids),
            "labels": list(self.labels),
            "query": self.query,
            "kind": self.kind,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Listing | None":
        if not isinstance(data, dict):
            return None
        ids = [int(value) for value in data.get("product_ids") or [] if value]
        if not ids:
            return None
        return cls(
            product_ids=ids,
            labels=[str(value) for value in data.get("labels") or []],
            query=str(data.get("query") or ""),
            kind=str(data.get("kind") or ""),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass
class TurnFrame:
    """Everything one turn needs to know, assembled before the NLU runs.

    L3.0 fills the fields the legacy pipeline already had and adds the empty
    slots (`last_listing`, `pending_offer`, `cache`, `evidence`) that L3.1+
    consume. Adding fields here is cheap; changing the persisted format twice
    is not.
    """

    conn: object
    conversation: dict
    turn: dict
    context: dict
    history: list[dict] = field(default_factory=list)
    turn_messages: list[dict] = field(default_factory=list)
    text: str = ""
    intent: object | None = None
    stage: str = ""
    focus_product: dict | None = None
    focus_source: str = ""
    last_listing: Listing | None = None
    pending_offer: dict | None = None
    active_sale: dict | None = None
    facts: dict[str, str] = field(default_factory=dict)
    profile: dict | None = None
    candidates: list[dict] = field(default_factory=list)
    references: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    cache: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)
    deadline: float | None = None
    turn_id: int | None = None

    @property
    def conversation_id(self) -> int:
        return int(self.conversation["id"])


def build_frame(
    conn,
    conversation,
    turn: dict,
    *,
    turn_messages: list[dict],
    text: str,
    context: dict,
    history: list[dict],
    focus_product: dict | None = None,
    focus_source: str = "",
) -> TurnFrame:
    """Assemble the turn state without changing any existing value."""
    active_sale = None
    try:
        active_sale = sales_domain.active_sale_for_conversation(
            conn, conversation["id"]
        )
    except Exception:  # noqa: BLE001 - assembling must never block a reply
        log.exception("could not load the active sale for conversation %s", conversation["id"])
    try:
        facts = memory.facts_map(conn, conversation["id"])
    except Exception:  # noqa: BLE001
        facts = {}
    profile = None
    customer_id = conversation.get("customer_id")
    if customer_id:
        try:
            profile = memory.customer_profile(conn, customer_id)
        except Exception:  # noqa: BLE001
            profile = None
    return TurnFrame(
        conn=conn,
        conversation=conversation,
        turn=turn,
        context=context,
        history=history,
        turn_messages=turn_messages,
        text=text,
        stage=str(context.get("stage") or "").upper(),
        focus_product=focus_product,
        focus_source=focus_source,
        last_listing=Listing.from_dict(context.get("last_listing")),
        pending_offer=context.get("pending_offer"),
        active_sale=active_sale,
        facts=facts,
        profile=profile,
        turn_id=turn.get("id"),
    )


def commit(
    frame: TurnFrame,
    *,
    intent,
    product,
    generator: str,
    escalate: bool,
    stage: str | None = None,
    sale: dict | None = None,
    sale_qty: int | None = None,
    clear_product: bool = False,
) -> None:
    """Persist the turn context; the single write point for `context_json`."""
    if frame.last_listing is not None:
        # Additive key: L3.1 reads it to resolve "esos" and "el segundo".
        frame.context["last_listing"] = frame.last_listing.as_dict()
    _update_context(
        frame.conn,
        frame.conversation,
        intent,
        product,
        generator,
        escalate,
        stage=stage,
        sale=sale,
        sale_qty=sale_qty,
        context=frame.context,
        clear_product=clear_product,
    )
    if stage:
        frame.stage = stage

