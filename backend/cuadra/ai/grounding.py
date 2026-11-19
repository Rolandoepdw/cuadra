"""Grounding: every factual claim needs evidence from *this* turn.

The guardrails already check price, specs and format. Grounding adds the piece
that caused the real-world bug: a negative claim ("no hay laptops") is only
allowed when a read in this turn actually returned empty, with a reason.

In L3.0 the validator is complete for price/specs (delegating to
`guardrails`) and the negative-claim skeleton is present but only runs when the
frame carries tool evidence. It is wired in `shadow`: violations are recorded,
the reply is not changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from cuadra.ai import guardrails

NEGATIVE_PATTERNS = (
    r"no\s+tenemos",
    r"no\s+hay",
    r"no\s+manejo",
    r"no\s+manejamos",
    r"no\s+me\s+queda",
    r"no\s+me\s+quedan",
    r"no\s+est[aá]\s+disponible",
    r"no\s+se\s+consigue",
    r"no\s+contamos\s+con",
    r"no\s+existe",
    r"\bagotado\b",
    r"\bsin\s+stock\b",
)
_NEGATIVE_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    kind: str
    text: str
    entity_id: int | None = None
    value: Any = None


@dataclass
class Violation:
    kind: str
    claim: Claim
    reason: str
    evidence_required: str = ""


@dataclass
class GroundingReport:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violations": [
                {
                    "kind": violation.kind,
                    "reason": violation.reason,
                    "claim": violation.claim.text,
                }
                for violation in self.violations
            ],
        }


def negative_claims(reply: str) -> list[Claim]:
    claims = []
    for match in _NEGATIVE_RE.finditer(reply or ""):
        start = max(0, match.start() - 40)
        end = min(len(reply), match.end() + 40)
        claims.append(
            Claim(
                kind="negative",
                text=reply[start:end].strip(),
                value=match.group(0).lower(),
            )
        )
    return claims


def _has_empty_evidence(frame, claim: Claim) -> bool:
    """A negative claim is grounded by an empty read with a reason."""
    for item in getattr(frame, "evidence", []) or []:
        empty_reason = (
            item.get("empty_reason")
            if isinstance(item, dict)
            else getattr(item, "empty_reason", None)
        )
        if empty_reason:
            return True
    return False


def validate_claims(
    reply: str,
    frame,
    guard_context: dict | None = None,
) -> GroundingReport:
    """Return the violations; never rewrite the reply here."""
    report = GroundingReport()
    context = guard_context or getattr(frame, "context", {}) or {}

    allowed = guardrails.allowed_prices(context)
    if not guardrails.check_prices(reply, allowed):
        report.violations.append(
            Violation(
                kind="price",
                claim=Claim(kind="price", text=reply),
                reason="price_not_in_turn",
                evidence_required="catalog.get / allowed_prices",
            )
        )

    if not guardrails.check_specs(reply, context):
        report.violations.append(
            Violation(
                kind="specs",
                claim=Claim(kind="specs", text=reply),
                reason="spec_not_in_corpus",
                evidence_required="catalog.get specifications",
            )
        )

    # L3.0: the skeleton only fires when this turn produced tool evidence.
    # Until L3.1 wires catalog reads into the frame, an empty `evidence` means
    # "not measurable", not "violation".
    if getattr(frame, "evidence", None):
        for claim in negative_claims(reply):
            if not _has_empty_evidence(frame, claim):
                report.violations.append(
                    Violation(
                        kind="negative",
                        claim=claim,
                        reason="negative_without_empty_read",
                        evidence_required="catalog.search with empty_reason",
                    )
                )
    return report
