"""Prompt construction with an explicit token budget (phase 1).

The budget is derived from the model context window instead of being an
independent number:

    budget = min(LLM_MAX_PROMPT_TOKENS, LLM_CTX - max_output - safety)

Everything below the rules is disposable: history is trimmed first, then the
candidate list, then the current turn text. The system prompt, the rolling
summary, the conversation state and the task instructions are never dropped.
"""

import json

from cuadra import config
from cuadra.ai import guardrails, llm, memory, prompts
from cuadra.domain import catalog, conversations, settings

# Spanish text is closer to 3.5 chars/token than the optimistic 4.
CHARS_PER_TOKEN = 3.5
MAX_MESSAGE_CHARS = 1200


def prompt_budget() -> int:
    """Tokens available for the prompt, validated against the real context."""
    derived = (
        config.LLM_CTX
        - config.LLM_MAX_OUTPUT_TOKENS
        - config.LLM_CONTEXT_SAFETY_TOKENS
    )
    if config.LLM_MAX_PROMPT_TOKENS > 0:
        derived = min(config.LLM_MAX_PROMPT_TOKENS, derived)
    return max(512, derived)


class ContextBuilder:
    def __init__(
        self,
        conn,
        conversation: dict,
        *,
        context: dict | None = None,
        budget: int | None = None,
    ):
        self.conn = conn
        self.conversation = conversation
        self.context = (
            conversations.get_context(conn, conversation["id"])
            if context is None
            else context
        )
        self.budget = budget or prompt_budget()

    # ---------------------------------------------------------------- system

    def _system(self, product, candidates) -> str:
        parts = [prompts.SYSTEM_PROMPT]

        # H-14: a legacy summary may still carry internal terms; never inject them.
        summary = guardrails.redact_internal_terms(self.context.get("summary") or "")
        if summary:
            parts.append("RESUMEN DE LA CONVERSACIÓN HASTA AHORA:\n" + summary)

        # Pinned facts and the customer profile are never trimmed: they are the
        # only memory that survives after the history window rolls over.
        facts = memory.facts_context(self.conn, self.conversation["id"])
        if facts:
            parts.append(
                "HECHOS FIJADOS DE ESTA CONVERSACIÓN (no los contradigas):\n"
                + json.dumps(facts, ensure_ascii=False)
            )
        customer_id = self.conversation.get("customer_id")
        if customer_id is not None:
            profile = memory.profile_context(self.conn, customer_id)
            if profile:
                parts.append(
                    "PERFIL DEL CLIENTE (memoria entre conversaciones):\n"
                    + json.dumps(profile, ensure_ascii=False)
                )

        state = []
        if self.context.get("last_intent"):
            state.append(f"Intención previa del cliente: {self.context['last_intent']}.")
        if self.context.get("pending_escalation"):
            state.append(
                "Hay una consulta pendiente al almacén: no prometas un dato que "
                "todavía no confirmó el proveedor."
            )
        # H-25: the state must mirror the product resolved for this turn, not the
        # one remembered from the previous turn (the PRODUCTO block was already
        # correct, which made the two sections contradict each other).
        turn_product_id = (
            product["id"] if product is not None else self.context.get("product_id")
        )
        if turn_product_id:
            state.append(f"Producto en juego (id {turn_product_id}).")
        if state:
            parts.append("ESTADO DE LA CONVERSACIÓN:\n" + "\n".join(state))

        if product is not None:
            parts.append(
                "PRODUCTO EN JUEGO (datos reales; no inventes otros):\n"
                + json.dumps(
                    catalog.llm_product_json(self.conn, product),
                    ensure_ascii=False,
                )
            )
        if product is not None or candidates:
            # H-23: history may still contain a price that was later changed or
            # reverted. The current catalog rows are the only source of truth.
            parts.append(
                "PRECIOS VIGENTES: usa solo los precios de PRODUCTO EN JUEGO y "
                "PRODUCTOS CANDIDATOS. Un precio dicho en un mensaje anterior "
                "puede haber cambiado: no lo repitas."
            )
        if candidates:
            parts.append(
                "PRODUCTOS CANDIDATOS:\n"
                + json.dumps(
                    [
                        catalog.llm_product_json(self.conn, candidate)
                        for candidate in candidates
                    ],
                    ensure_ascii=False,
                )
            )
        # H-02: the schema has no per-product transport field, so the business
        # policy is the only source of truth. Never let the model invent it.
        policy = settings.transport_policy(self.conn)
        parts.append("TRANSPORTE: " + prompts.TRANSPORT_CLAUSES[policy].strip())
        return "\n\n".join(parts)

    # --------------------------------------------------------------- history

    @staticmethod
    def _history_messages(history: list[dict], allowed=None) -> list[dict]:
        messages = []
        for message in history:
            role = "user" if message["direction"] == "INBOUND" else "assistant"
            body = message["body"] or "[" + message["type"] + "]"
            # H-23: only the bot's own words are rewritten; the customer's
            # history must stay verbatim.
            if role == "assistant" and allowed is not None:
                body = guardrails.redact_stale_prices(body, allowed)
            messages.append(
                {"role": role, "content": body[:MAX_MESSAGE_CHARS]}
            )
        return messages

    @staticmethod
    def _tokens(messages: list[dict]) -> int:
        return llm.estimate_tokens(messages)

    # ----------------------------------------------------------------- build

    def build(
        self,
        history: list[dict],
        *,
        product=None,
        candidates=None,
        turn_text: str = "",
        instruction_tokens: int = 140,
    ) -> list[dict]:
        """Return ``[system, ...history]`` that fits the budget."""
        messages, _ = self.build_with_turn(
            history,
            product=product,
            candidates=candidates,
            turn_text=turn_text,
            instruction_tokens=instruction_tokens,
        )
        return messages

    def build_with_turn(
        self,
        history: list[dict],
        *,
        product=None,
        candidates=None,
        turn_text: str = "",
        instruction_tokens: int = 140,
    ) -> tuple[list[dict], str]:
        """Return ``([system, ...history], turn_text)`` that fits the budget.

        The turn text is measured but not appended: `llm.classify` and
        `llm.compose` add it and their task instructions themselves. It is
        returned (possibly truncated) so the caller sends the same text that
        was budgeted.
        """
        candidates = list(candidates or [])
        system = self._system(product, candidates)
        head = {"role": "system", "content": system}
        tail = {"role": "user", "content": turn_text or ""}

        # H-23: the bot may only quote prices that exist in this context, so any
        # other amount in its own history is stale (an old or reverted price).
        allowed = guardrails.allowed_prices(
            {
                "product": product,
                "candidates": candidates,
                "quoted_prices": self.context.get("quoted_prices") or [],
            }
        )
        recent = self._history_messages(history, allowed)[-config.HISTORY_WINDOW:]
        fixed = self._tokens([head]) + self._tokens([tail]) + instruction_tokens
        while recent and fixed + self._tokens(recent) > self.budget:
            recent = recent[1:]

        # Still over budget: shrink the candidate list before touching rules.
        while candidates and fixed + self._tokens(recent) > self.budget:
            candidates = candidates[:-1]
            head = {"role": "system", "content": self._system(product, candidates)}
            fixed = self._tokens([head]) + self._tokens([tail]) + instruction_tokens

        # Last resort: truncate the turn text; never the rules or the task.
        overflow = fixed + self._tokens(recent) - self.budget
        if overflow > 0 and tail["content"]:
            keep_chars = max(200, int(len(tail["content"]) - overflow * CHARS_PER_TOKEN))
            tail["content"] = tail["content"][:keep_chars]

        return [head, *recent], tail["content"]
