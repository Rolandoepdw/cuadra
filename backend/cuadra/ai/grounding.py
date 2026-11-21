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

# "No hay problema" is politeness, not a claim about the catalog.
_NEGATIVE_BENIGN = re.compile(
    r"\b(?:problema|de\s+qu[eé]|compromiso|inconveniente|pena|pero)\b",
    re.IGNORECASE,
)

# L3.2: order status, coverage and delivery dates are claims about the world.
_STATUS_RE = re.compile(
    r"\bV-\d{6}\b"
    r"|\btu\s+pedido\b[^.\n]{0,60}\b(?:registrad[oa]|confirmad[oa]|despachad[oa]|"
    r"entregad[oa]|cancelad[oa]|pendiente|preparaci[oó]n|en\s+camino)\b"
    r"|\b(?:pedido|orden)\b[^.\n]{0,30}\b(?:registrad[oa]|confirmad[oa]|"
    r"despachad[oa]|entregad[oa]|cancelad[oa])\b",
    re.IGNORECASE,
)
_COVERAGE_RE = re.compile(
    r"\b(?:entregamos|no\s+entregamos|no\s+llegamos|cubre|no\s+cubre|cobertura)\b"
    r"|\b(?:esa|esta|la)\s+zona\b[^.\n]{0,20}\b(?:cubre|llega|entregamos)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:te\s+)?(?:llega|llegar[aá]|lo\s+llevo|la\s+llevo)\b[^.\n]{0,25}"
    r"\b(?:mañana|pasado\s+mañana|hoy|el\s+(?:lunes|martes|mi[eé]rcoles|"
    r"jueves|viernes|s[aá]bado|domingo)|en\s+\d+\s+(?:d[ií]as|horas)|"
    r"esta\s+semana|pr[oó]xima\s+semana|el\s+\d{1,2})\b"
    r"|\bentrega\b[^.\n]{0,20}\b(?:en\s+\d+\s+(?:d[ií]as|horas)|mañana|"
    r"el\s+(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo))\b",
    re.IGNORECASE,
)

# L3.3: claims about what the customer already said need the fact read. A
# correction ("mi dirección es otra") never matches these: it is not a claim
# about the past, so grounding can never block it.
_FACT_MENTION = re.compile(
    r"\b(?:ya\s+me\s+(?:dijiste|diste|pasaste|compartiste|confirmaste)|"
    r"me\s+dijiste|te\s+dije|recuerdo\s+que|"
    r"seg[uú]n\s+lo\s+que\s+me\s+dijiste|ya\s+tengo\s+tu)\b",
    re.IGNORECASE,
)
_FACT_NEGATION = re.compile(
    r"\b(?:no\s+me\s+(?:has\s+)?(?:dicho|diste|pasado|compartido|confirmado)|"
    r"a[uú]n\s+no\s+me\s+(?:has\s+)?(?:dicho|diste)|"
    r"todav[ií]a\s+no\s+me\s+(?:has\s+)?(?:dicho|diste))\b",
    re.IGNORECASE,
)
_FACT_KEYWORDS = (
    ("delivery_address", (r"\bdirecci[oó]n\b",)),
    ("customer_name", (r"\bnombre\b",)),
    ("customer_phone", (r"\btel[eé]fono\b", r"\bcelular\b", r"\bm[oó]vil\b")),
    ("zone_name", (r"\bzona\b",)),
    ("budget", (r"\bpresupuesto\b",)),
)


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
        window_start = max(0, match.start() - 40)
        window_end = min(len(reply), match.end() + 40)
        fragment = reply[window_start:window_end]
        after = reply[match.end(): match.end() + 25]
        if match.group(0).lower().startswith("no hay") and _NEGATIVE_BENIGN.search(
            after
        ):
            continue
        claims.append(
            Claim(
                kind="negative",
                text=fragment.strip(),
                value=match.group(0).lower(),
            )
        )
    return claims


def _field(item, key):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _has_empty_evidence(frame, claim: Claim) -> bool:
    """A negative claim is grounded by an empty read or a zero-stock read."""
    for item in getattr(frame, "evidence", []) or []:
        if _field(item, "empty_reason"):
            return True
        if _field(item, "availability") == "out_of_stock":
            return True
        if _field(item, "available") is False:
            return True
        stock = _field(item, "stock")
        if stock is not None and int(stock) <= 0:
            return True
    return False


def _has_tool(frame, *tools: str) -> bool:
    for item in getattr(frame, "evidence", []) or []:
        if _field(item, "tool") in tools:
            return True
    return False


def _has_tcp_eta(frame) -> bool:
    for item in getattr(frame, "evidence", []) or []:
        if _field(item, "tool") == "logistics.eta" and _field(item, "source") == "tcp":
            return True
    return False


def _fact_keys(text: str) -> list[str]:
    keys = []
    for key, patterns in _FACT_KEYWORDS:
        if any(re.search(pattern, text or "", re.IGNORECASE) for pattern in patterns):
            keys.append(key)
    return keys


def _present_facts(frame) -> set[str] | None:
    """Keys the turn's `memory.facts` read returned; None means not measurable."""
    for item in getattr(frame, "evidence", []) or []:
        if _field(item, "tool") == "memory.facts":
            return set(_field(item, "present") or [])
    return None


def _claim_text(reply: str, pattern) -> str:
    match = pattern.search(reply or "")
    if not match:
        return reply or ""
    start = max(0, match.start() - 30)
    end = min(len(reply), match.end() + 30)
    return reply[start:end].strip()


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

    # L3.2: a status, coverage or date statement needs the read that backs it.
    if _STATUS_RE.search(reply or "") and not _has_tool(frame, "sales.status"):
        report.violations.append(
            Violation(
                kind="status",
                claim=Claim(kind="status", text=_claim_text(reply, _STATUS_RE)),
                reason="status_without_sale_read",
                evidence_required="sales.status",
            )
        )
    if _COVERAGE_RE.search(reply or "") and not _has_tool(
        frame, "zones.check", "zones.list"
    ):
        report.violations.append(
            Violation(
                kind="coverage",
                claim=Claim(kind="coverage", text=_claim_text(reply, _COVERAGE_RE)),
                reason="coverage_without_zone_read",
                evidence_required="zones.check",
            )
        )
    if _DATE_RE.search(reply or "") and not _has_tcp_eta(frame):
        report.violations.append(
            Violation(
                kind="date",
                claim=Claim(kind="date", text=_claim_text(reply, _DATE_RE)),
                reason="date_without_tcp_eta",
                evidence_required="logistics.eta source=tcp",
            )
        )

    # L3.3: "ya me dijiste X" needs X present; "no me has dicho X" needs it absent.
    present = _present_facts(frame)
    if present is not None:
        mention = _FACT_MENTION.search(reply or "")
        negation = _FACT_NEGATION.search(reply or "")
        if mention or negation:
            expected = not bool(negation)
            pattern = _FACT_NEGATION if negation else _FACT_MENTION
            for key in _fact_keys(reply or ""):
                if (key in present) == expected:
                    continue
                report.violations.append(
                    Violation(
                        kind="memory",
                        claim=Claim(
                            kind="memory", text=_claim_text(reply, pattern)
                        ),
                        reason="fact_claim_not_supported",
                        evidence_required=f"memory.facts:{key}",
                    )
                )
    return report
