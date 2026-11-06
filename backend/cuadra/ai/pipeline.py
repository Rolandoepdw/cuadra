"""Incoming-turn pipeline: context -> NLU -> rules -> guardrails -> reply.

The LLM classifies intent (JSON). The *engine* here decides the answer by rules
and templates (doc 01 §6, doc 03 §2-3); free generation is only used for
comparisons, complaints and unforeseen cases, always fed with real catalog JSON
and always validated before it reaches the customer.
"""

import json
import logging
import re
import time

from cuadra import config
from cuadra.ai import guardrails, llm, memory, prompts, retrieval
from cuadra.ai.context import ContextBuilder
from cuadra.domain import catalog, conversations, events, turns

log = logging.getLogger("cuadra.pipeline")


def _build_base_messages(
    conn,
    conversation,
    history: list[dict],
    context: dict,
    *,
    product: dict | None = None,
    candidates: list[dict] | None = None,
    turn_text: str = "",
) -> tuple[list[dict], str]:
    return ContextBuilder(conn, conversation, context=context).build_with_turn(
        history,
        product=product,
        candidates=candidates,
        turn_text=turn_text,
    )


_ID_PREFIXED = re.compile(
    r"\b(?:id|producto|codigo|código)\s*[:#]?\s*(\d{1,9})\b", re.IGNORECASE
)
_ID_BARE = re.compile(r"^\s*#?(\d{1,6})\s*$")


def product_id_from_text(text: str) -> int | None:
    """Return an explicit product id, so the bot never has to guess from a photo."""
    body = (text or "").strip()
    match = _ID_BARE.match(body) or _ID_PREFIXED.search(body)
    return int(match.group(1)) if match else None


def _retrieve_candidates(conn, query: str, *, limit: int = 4) -> list[dict]:
    """Hybrid retrieval (FTS/embeddings/RRF, fuzzy fallback) as real product rows."""
    products: list[dict] = []
    for product_id in retrieval.search_ids(conn, query, top_k=limit):
        try:
            product = catalog.get_product(conn, product_id)
        except catalog.NotFound:
            continue
        if product["status"] in ("ACTIVE", "OUT_OF_STOCK"):
            products.append(product)
    return products


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
        # Fuzzy first: it is precise for the exact names the NLU usually emits.
        product = catalog.find_product(conn, intent.producto)
        if product is not None:
            return product
        # Semantic/lexical fallback for paraphrases the fuzzy matcher misses.
        candidates = _retrieve_candidates(conn, intent.producto, limit=1)
        if candidates:
            return candidates[0]
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
        zona=info["zona_entrega"] or "tu zona",
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


def _guard_context(
    product: dict | None,
    candidates: list[dict] | None,
    context: dict,
    *,
    extra_products: list[dict] | None = None,
) -> dict:
    products = []
    for value in (product, *(candidates or []), *(extra_products or [])):
        if value is not None:
            products.append(value)
    return {
        "product": product,
        "products": products,
        "quoted_prices": context.get("quoted_prices") or [],
    }


def _log_guardrail(conn, conversation, reason: str, stage: str) -> None:
    events.log(
        conn,
        "llm",
        "guardrail_violation",
        entity="conversations",
        entity_id=conversation["id"],
        details={"reason": reason, "stage": stage},
    )
    log.warning(
        "guardrail violation (%s/%s) in conversation %s",
        stage,
        reason,
        conversation["id"],
    )


def _safe_template(conn, conversation, reply: str, guard_context: dict, generator: str):
    ok, reason = guardrails.validate_reply(reply, guard_context)
    if ok:
        return reply, generator
    _log_guardrail(conn, conversation, reason, "template")
    return guardrails.fallback_reply(guard_context), "fallback"


async def _decide(
    conn,
    conversation,
    inbound: dict,
    intent,
    product: dict | None,
    context: dict,
    history: list[dict],
    *,
    last_text: str | None = None,
) -> tuple[str, bool, str, str | None]:
    """Returns (reply, escalate, generator, escalation_question)."""
    text = last_text if last_text is not None else (inbound["body"] or "")

    if intent.intencion in ("consulta_precio", "consulta_stock", "comprar"):
        if product is None:
            guard_context = _guard_context(None, None, context)
            reply, generator = _safe_template(
                conn,
                conversation,
                prompts.TEMPLATES["ask_which_product"],
                guard_context,
                "template",
            )
            return reply, False, generator, None
        if product["status"] != "ACTIVE" or (product.get("stock") or 0) <= 0:
            alternatives = catalog.alternatives(conn, product)
            guard_context = _guard_context(
                product, None, context, extra_products=alternatives
            )
            reply, generator = _safe_template(
                conn,
                conversation,
                _render_out_of_stock(alternatives),
                guard_context,
                "template",
            )
            return reply, False, generator, None
        guard_context = _guard_context(product, None, context)
        reply = _render_greeting(conn, product)
        if intent.intencion == "comprar":
            canonical = catalog.llm_product_json(conn, product)
            reply += f" ¿Te reservo el {canonical['nombre']} y coordinamos la entrega?"
        reply, generator = _safe_template(
            conn, conversation, reply, guard_context, "template"
        )
        return reply, False, generator, None

    if intent.intencion == "consulta_dato":
        missing = ", ".join(intent.datos_faltantes) if intent.datos_faltantes else text
        guard_context = _guard_context(product, None, context)
        reply, generator = _safe_template(
            conn,
            conversation,
            prompts.TEMPLATES["missing_data_escalate"],
            guard_context,
            "template",
        )
        return reply, True, generator, missing

    # comparar / queja / otro / low confidence -> careful free generation
    candidates = None
    if intent.intencion == "comparar":
        candidates = _retrieve_candidates(conn, intent.producto or text, limit=4)
    messages, prompt_text = _build_base_messages(
        conn,
        conversation,
        history,
        context,
        product=product,
        candidates=candidates,
        turn_text=text,
    )
    extra = ""
    if candidates:
        extra = "El cliente compara productos: usa solo los PRODUCTOS CANDIDATOS que tienes."
    guard_context = _guard_context(product, candidates, context)

    generator = "llm"
    reply = await llm.compose(
        messages, prompt_text, extra=extra, conversation_id=conversation["id"]
    )
    ok, reason = guardrails.validate_reply(reply, guard_context)
    if not ok:
        _log_guardrail(conn, conversation, reason, "retry")
        correction = "\n".join(
            part for part in (extra, prompts.GUARDRAIL_CORRECTION) if part
        )
        reply = await llm.compose(
            messages,
            prompt_text,
            extra=correction,
            conversation_id=conversation["id"],
        )
        ok, reason = guardrails.validate_reply(reply, guard_context)
        if not ok:
            _log_guardrail(conn, conversation, reason, "fallback")
            reply = guardrails.fallback_reply(guard_context)
            generator = "fallback"
    return reply, False, generator, None


def _update_context(conn, conversation, intent, product, generator: str, escalate: bool) -> None:
    context = conversations.get_context(conn, conversation["id"])
    if product is not None:
        context["product_id"] = product["id"]
    context["last_intent"] = intent.intencion
    context["last_generator"] = generator
    context["pending_escalation"] = bool(escalate)
    conversations.set_context(conn, conversation["id"], context)


def _turn_messages(conn, turn: dict) -> list[dict]:
    return conversations.get_messages(conn, turns.message_ids(turn))


def _turn_text(messages: list[dict]) -> str:
    parts = []
    for message in messages:
        if message["direction"] != "INBOUND":
            continue
        body = (message["body"] or "").strip()
        if body:
            parts.append(body)
        elif message["type"] == "image":
            parts.append("[imagen enviada]")
        elif message["type"] == "audio":
            parts.append("[audio]")
    return "\n".join(parts).strip()


async def _classify(conn, conversation, messages: list[dict], text: str, message_id):
    try:
        return (
            await llm.classify(
                messages, text, conversation_id=conversation["id"]
            ),
            False,
        )
    except llm.LLMInvalidOutput as exc:
        log.warning("NLU degraded to intent=otro: %s", exc)
        events.log(
            conn,
            "llm",
            "nlu_invalid_output",
            entity="conversations",
            entity_id=conversation["id"],
            details={"message_id": message_id},
        )
        return llm.Intent(intencion="otro", confianza=0.0), True


async def handle_turn(conn, conversation, turn: dict) -> dict:
    """Run the full pipeline once for a consolidated customer turn."""
    started = time.perf_counter()
    turn_messages = _turn_messages(conn, turn)
    turn_ids = {message["id"] for message in turn_messages}
    last_message_id = (
        turn_messages[-1]["id"] if turn_messages else turn.get("last_message_id")
    )
    text = _turn_text(turn_messages) or "[mensaje sin texto]"

    context = conversations.get_context(conn, conversation["id"])
    history = [
        message
        for message in conversations.recent_messages(
            conn,
            conversation["id"],
            config.HISTORY_WINDOW + len(turn_ids) + 4,
        )
        if message["id"] not in turn_ids
    ]

    current_product = _resolve_product(
        conn, llm.Intent(intencion="otro"), context, text
    )
    # Deterministic facts are written before the prompt is built so a zone,
    # budget or name mentioned now survives even if the summary is not due yet.
    memory.capture_facts(
        conn,
        conversation,
        text,
        product=current_product,
        source_message_id=last_message_id,
    )
    nlu_messages, prompt_text = _build_base_messages(
        conn,
        conversation,
        history,
        context,
        product=current_product,
        turn_text=text,
    )

    intent, degraded = await _classify(
        conn, conversation, nlu_messages, prompt_text, last_message_id
    )

    product = _resolve_product(conn, intent, context, text)
    if product is not None and product_id_from_text(text) is not None:
        if intent.intencion == "otro":
            intent.intencion = "consulta_precio"
            intent.confianza = max(intent.confianza, 0.9)
    memory.capture_facts(
        conn,
        conversation,
        text,
        product=product,
        source_message_id=last_message_id,
    )

    reply, escalate, generator, escalation_question = await _decide(
        conn,
        conversation,
        {"body": text, "id": last_message_id},
        intent,
        product,
        context,
        history,
        last_text=text,
    )
    supplier_id = product["supplier_id"] if (escalate and product) else None

    _update_context(conn, conversation, intent, product, generator, escalate)
    return {
        "turn_id": turn.get("id"),
        "message_id": last_message_id,
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
        "context_tokens": llm.estimate_tokens(
            [*nlu_messages, {"role": "user", "content": prompt_text}]
        ),
    }


async def handle_incoming(conn, conversation, inbound: dict) -> dict:
    """Backwards-compatible single-message entry point (legacy jobs/debug)."""
    turn = {
        "id": None,
        "conversation_id": conversation["id"],
        "last_message_id": inbound["id"],
        "message_ids_json": json.dumps([inbound["id"]]),
    }
    return await handle_turn(conn, conversation, turn)


async def maybe_summarize(conn, conversation) -> bool:
    """Backwards-compatible wrapper around the phase 2 memory module."""
    return await memory.maybe_summarize(conn, conversation)
