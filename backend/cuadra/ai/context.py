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
from cuadra.ai import llm, prompts
from cuadra.domain import catalog, conversations

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

        summary = self.context.get("summary")
        if summary:
            parts.append("RESUMEN DE LA CONVERSACIÓN HASTA AHORA:\n" + summary)

        state = []
        if self.context.get("last_intent"):
            state.append(f"Intención previa del cliente: {self.context['last_intent']}.")
        if self.context.get("pending_escalation"):
            state.append(
                "Hay una consulta pendiente al almacén: no prometas un dato que "
                "todavía no confirmó el proveedor."
            )
        if self.context.get("product_id"):
            state.append(
                f"Producto en juego (id {self.context['product_id']})."
            )
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
        return "\n\n".join(parts)

    # --------------------------------------------------------------- history

    @staticmethod
    def _history_messages(history: list[dict]) -> list[dict]:
        messages = []
        for message in history:
            role = "user" if message["direction"] == "INBOUND" else "assistant"
            body = message["body"] or "[" + message["type"] + "]"
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

        recent = self._history_messages(history)[-config.HISTORY_WINDOW:]
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
