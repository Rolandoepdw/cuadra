"""Incoming-message pipeline: context -> NLU -> rules -> reply.

The LLM classifies intent (JSON). The *engine* here decides the answer by rules
and templates (doc 01 §6, doc 03 §2-3); free generation is only used for
comparisons, complaints and unforeseen cases, always fed with real catalog JSON.
"""

import json
import logging
import re
import time

from cuadra import config
from cuadra.ai import llm, prompts
from cuadra.domain import catalog, conversations, events

log = logging.getLogger("cuadra.pipeline")


def _build_base_messages(
    conn,
    conversation,
    history: list[dict],
    context: dict,
    *,
    product: dict | None = None,
    candidates: list[dict] | None = None,
) -> list[dict]:
    parts = [prompts.SYSTEM_PROMPT]
    if context.get("summary"):
        parts.append("RESUMEN DE LA CONVERSACIÓN HASTA AHORA:\n" + context["summary"])
    if product is not None:
        parts.append(
            "PRODUCTO EN JUEGO (datos reales; no inventes otros):\n"
            + json.dumps(catalog.llm_product_json(conn, product), ensure_ascii=False)
        )
    if candidates:
        parts.append(
            "PRODUCTOS CANDIDATOS:\n"
            + json.dumps(
                [catalog.llm_product_json(conn, c) for c in candidates], ensure_ascii=False
            )
        )
    messages = [{"role": "system", "content": "\n\n".join(parts)}]
    for message in history:
        role = "user" if message["direction"] == "INBOUND" else "assistant"
        messages.append({"role": role, "content": message["body"] or "[" + message["type"] + "]"})
    return messages


def _fit_budget(messages: list[dict]) -> list[dict]:
    """Keep the prompt under the token budget, dropping oldest history first."""
    if llm.estimate_tokens(messages) <= config.LLM_MAX_PROMPT_TOKENS:
        return messages
    system, rest = messages[0], messages[1:]
    keep = rest[-config.HISTORY_WINDOW:]
    while len(keep) > 2 and llm.estimate_tokens([system, *keep]) > config.LLM_MAX_PROMPT_TOKENS:
        keep = keep[1:]
    return [system, *keep]


_ID_PREFIXED = re.compile(
    r"\b(?:id|producto|codigo|código)\s*[:#]?\s*(\d{1,9})\b", re.IGNORECASE
)
_ID_BARE = re.compile(r"^\s*#?(\d{1,6})\s*$")


def product_id_from_text(text: str) -> int | None:
    """Return an explicit product id, so the bot never has to guess from a photo."""
    body = (text or "").strip()
    match = _ID_BARE.match(body) or _ID_PREFIXED.search(body)
    return int(match.group(1)) if match else None


def _resolve_product(
    conn, intent, context: dict, text: str = ""
) -> dict | None:
    explicit_id = product_id_from_text(text)
    if explicit_id is not None:
        try:
            return catalog.get_product(conn, explicit_id)
        except catalog.NotFound:
            if _ID_PREFIXED.search(text):
                return None
    if intent.producto:
        product = catalog.find_product(conn, intent.producto)
        if product is not None:
            return product
    if context.get("product_id"):
        try:
            return catalog.get_product(conn, context["product_id"])
        except catalog.NotFound:
            return None
    return None


def _render_greeting(conn, product: dict) -> str:
    info = catalog.llm_product_json(conn, product)
    return prompts.TEMPLATES["greeting_product"].format(
        producto=info["nombre"],
        precio=f"{info['precio']:g}",
        moneda=info["moneda"],
        transporte_clause="",
        zona=info["zona_entrega"] or "La Habana",
        zona_clause="",
    )


def _render_out_of_stock(alternatives: list[dict]) -> str:
    if not alternatives:
        return "Ese se nos fue por ahora. ¿Quieres que te avise cuando vuelva a entrar?"
    first = alternatives[0]
    if len(alternatives) == 1:
        return (
            f"Ese se nos fue por ahora. Tengo {first['name']} en "
            f"{first['price']:g} {first['currency']} que es parecido. ¿Te echo un vistazo?"
        )
    second = alternatives[1]
    return (
        f"Ese se nos fue por ahora. Tengo {first['name']} en {first['price']:g} {first['currency']} "
        f"y {second['name']} en {second['price']:g} {second['currency']}. "
        "¿Te echo un vistazo a alguna?"
    )


async def _decide(
    conn,
    conversation,
    inbound: dict,
    intent,
    product: dict | None,
    context: dict,
    history: list[dict],
) -> tuple[str, bool, str, str | None]:
    """Returns (reply, escalate, generator, escalation_question)."""
    last_text = inbound["body"] or ""

    if intent.intencion in ("consulta_precio", "consulta_stock", "comprar"):
        if product is None:
            return prompts.TEMPLATES["ask_which_product"], False, "template", None
        if product["status"] != "ACTIVE" or (product.get("stock") or 0) <= 0:
            return _render_out_of_stock(catalog.alternatives(conn, product)), False, "template", None
        reply = _render_greeting(conn, product)
        if intent.intencion == "comprar":
            canonical = catalog.llm_product_json(conn, product)
            reply += f" ¿Te reservo el {canonical['nombre']} y coordinamos la entrega?"
        return reply, False, "template", None

    if intent.intencion == "consulta_dato":
        missing = ", ".join(intent.datos_faltantes) if intent.datos_faltantes else last_text
        return prompts.TEMPLATES["missing_data_escalate"], True, "template", missing

    # comparar / queja / otro / low confidence -> careful free generation
    candidates = None
    if intent.intencion == "comparar":
        candidates = catalog.search_products(conn, intent.producto or last_text, limit=4)
    messages = _fit_budget(
        _build_base_messages(
            conn, conversation, history, context, product=product, candidates=candidates
        )
    )
    extra = ""
    if candidates:
        extra = "El cliente compara productos: usa solo los PRODUCTOS CANDIDATOS que tienes."
    reply = await llm.compose(
        messages, last_text, extra=extra, conversation_id=conversation["id"]
    )
    return reply, False, "llm", None


def _update_context(conn, conversation, intent, product, generator: str, escalate: bool) -> None:
    context = conversations.get_context(conn, conversation["id"])
    if product is not None:
        context["product_id"] = product["id"]
    context["last_intent"] = intent.intencion
    context["last_generator"] = generator
    context["pending_escalation"] = bool(escalate)
    conversations.set_context(conn, conversation["id"], context)


async def handle_incoming(conn, conversation, inbound: dict) -> dict:
    """Run the full pipeline for one stored inbound message. Returns the reply."""
    started = time.perf_counter()
    context = conversations.get_context(conn, conversation["id"])
    history = conversations.recent_messages(conn, conversation["id"], config.HISTORY_WINDOW)
    history_base = [m for m in history if m["id"] != inbound["id"]]
    message_text = inbound["body"] or ""

    current_product = _resolve_product(
        conn, llm.Intent(intencion="otro"), context, message_text
    )
    nlu_messages = _fit_budget(
        _build_base_messages(
            conn, conversation, history_base, context, product=current_product
        )
    )

    degraded = False
    try:
        intent = await llm.classify(
            nlu_messages, inbound["body"] or "", conversation_id=conversation["id"]
        )
    except llm.LLMInvalidOutput as exc:
        log.warning("NLU degraded to intent=otro: %s", exc)
        events.log(
            conn,
            "llm",
            "nlu_invalid_output",
            entity="conversations",
            entity_id=conversation["id"],
            details={"message_id": inbound["id"]},
        )
        intent = llm.Intent(intencion="otro", confianza=0.0)
        degraded = True

    product = _resolve_product(conn, intent, context, message_text)
    if product is not None and product_id_from_text(message_text) is not None:
        if intent.intencion == "otro":
            intent.intencion = "consulta_precio"
            intent.confianza = max(intent.confianza, 0.9)
    reply, escalate, generator, escalation_question = await _decide(
        conn, conversation, inbound, intent, product, context, history
    )
    supplier_id = product["supplier_id"] if (escalate and product) else None

    _update_context(conn, conversation, intent, product, generator, escalate)
    return {
        "reply": reply,
        "intent": intent.model_dump(),
        "degraded": degraded,
        "product_id": product["id"] if product else None,
        "product_name": product["name"] if product else None,
        "supplier_id": supplier_id,
        "escalate": escalate,
        "escalation_question": escalation_question,
        "generator": generator,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "context_tokens": llm.estimate_tokens(nlu_messages),
    }


async def maybe_summarize(conn, conversation) -> None:
    """Compress old history into the rolling summary every SUMMARY_EVERY messages."""
    count = conversations.message_count(conn, conversation["id"])
    context = conversations.get_context(conn, conversation["id"])
    through = int(context.get("summary_through") or 0)
    if count - through < config.SUMMARY_EVERY or count < config.HISTORY_WINDOW:
        return
    old = conversations.messages_before_window(conn, conversation["id"], config.HISTORY_WINDOW)
    if not old:
        return
    try:
        summary = await llm.summarize(
            context.get("summary"), old, conversation_id=conversation["id"]
        )
    except (llm.LLMUnavailable, llm.LLMInvalidOutput):
        return  # summary is an optimization: never break the reply because of it
    conversations.set_summary(conn, conversation["id"], summary)
    events.log(
        conn,
        "llm",
        "summary_updated",
        entity="conversations",
        entity_id=conversation["id"],
        details={"messages": count},
    )
