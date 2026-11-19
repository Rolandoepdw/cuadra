"""Prompt building, LLM composition and fallback templates."""

import logging
from typing import Callable

from cuadra.ai import guardrails, llm, prompts
from cuadra.ai.context import ContextBuilder

log = logging.getLogger("cuadra.composer")

from cuadra.ai.turn.guards import (
    _guard_context,
    _log_guardrail,
    _safe_template,
)


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


def _fast_template(
    conn, conversation, key: str, guard_context: dict, *, context=None, **values
):
    """Render a template with anti-repetition variants.

    ``context`` is the turn's context dict: without it the variant choice cannot
    be remembered (F5, prueba #2), so callers inside a turn must pass it.
    """
    if context is not None:
        text = prompts.template_variant(key, context, **values)
    else:
        text = prompts.TEMPLATES[key].format(**values)
    return _safe_template(conn, conversation, text, guard_context, "template")


async def _compose_open(
    conn,
    conversation,
    history: list[dict],
    context: dict,
    product: dict | None,
    text: str,
    *,
    extra: str = "",
    candidates: list[dict] | None = None,
    guard_context: dict | None = None,
    fallback: Callable[[], tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Compose a free-form reply for an open question (F10, prueba #2).

    Plantillas para los pasos transaccionales; el compositor solo para lo que
    una plantilla no puede contestar (un detalle del producto, una queja, una
    comparación, un tema abierto). Los guardrails corren en cada intento y, si
    el modelo no está disponible o insiste en violarlos, responde el fallback
    determinista en vez de dejar al cliente sin respuesta.
    """
    messages, prompt_text = _build_base_messages(
        conn,
        conversation,
        history,
        context,
        product=product,
        candidates=candidates,
        turn_text=text,
    )
    guard_context = guard_context or _guard_context(product, candidates, context)

    def _fallback() -> tuple[str, str]:
        # Se construye solo si hace falta: una plantilla de reserva no debe
        # consumir una variante ni dejar rastro en el contexto del turno.
        if fallback is not None:
            return fallback()
        return guardrails.fallback_reply(guard_context), "fallback"

    try:
        reply = await llm.compose(
            messages, prompt_text, extra=extra, conversation_id=conversation["id"]
        )
    except llm.LLMUnavailable:
        log.warning("compose unavailable; using deterministic reply")
        return _fallback()

    ok, reason = guardrails.validate_reply(reply, guard_context)
    if not ok:
        _log_guardrail(conn, conversation, reason, "retry")
        correction = "\n".join(
            part for part in (extra, prompts.GUARDRAIL_CORRECTION) if part
        )
        try:
            reply = await llm.compose(
                messages,
                prompt_text,
                extra=correction,
                conversation_id=conversation["id"],
            )
        except llm.LLMUnavailable:
            log.warning("compose unavailable on retry; using deterministic reply")
            return _fallback()
        ok, reason = guardrails.validate_reply(reply, guard_context)
        if not ok:
            _log_guardrail(conn, conversation, reason, "fallback")
            return _fallback()
    return reply, "llm"
