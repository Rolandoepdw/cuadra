"""Phase 0 baseline: run the golden set against a local LLM.

The evaluator is deliberately offline and synthetic: it never reads the real
database, never sends Telegram messages and never writes production traces.
It only talks to a llama.cpp/OpenAI-compatible server with the same prompts
Cuadra uses at runtime.

Usage (from ``backend/``):

    .venv\\Scripts\\python.exe scripts\\eval_agent.py --model bonsai
    .venv\\Scripts\\python.exe scripts\\eval_agent.py --tag nemotron --limit 10

Outputs a summary to stdout and a detailed JSON report in ``data/logs/``.
"""

import argparse
import hashlib
import json
import pathlib
import re
import statistics
import sys
import time
from types import SimpleNamespace

import httpx

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from cuadra.ai import grounding, prompts  # noqa: E402
from cuadra.ai.llm import INTENT_SCHEMA, Intent  # noqa: E402
from cuadra.ai.turn.frame import Listing  # noqa: E402
from cuadra.ai.turn.references import resolve_reference  # noqa: E402
from cuadra import config  # noqa: E402

GOLDEN_DIR = BACKEND / "tests" / "golden"
KNOWN_PRICES = {3.0, 12.5, 20.0}
BOT_WORDS = (
    "soy una ia",
    "asistente virtual",
    "inteligencia artificial",
    "chatbot",
    "soy un bot",
)
LEAK_WORDS = ("comision", "comisión", "margen", "costo", "proveedor", "prompt", "instrucciones")
REFUSAL = ("no ", "sin ", "nunca", "jamas", "jamás", "no puedo", "no tengo")


def prompt_fingerprint() -> str:
    """Short hash of the production prompts, so two runs are comparable."""
    parts = [prompts.SYSTEM_PROMPT, prompts.NLU_TASK, prompts.COMPOSE_INSTRUCTION]
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def load_jsonl(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_messages(
    case: dict, products: dict, *, compose: bool
) -> list[dict]:
    parts = [prompts.SYSTEM_PROMPT]
    product = products.get(case.get("product") or "")
    candidates = case.get("candidates") or []
    if product is not None:
        parts.append(
            "PRODUCTO EN JUEGO (datos reales; no inventes otros):\n"
            + json.dumps(product, ensure_ascii=False)
        )
    if candidates:
        parts.append(
            "PRODUCTOS CANDIDATOS:\n"
            + json.dumps([products[c] for c in candidates], ensure_ascii=False)
        )
    task = prompts.COMPOSE_INSTRUCTION if compose else prompts.NLU_TASK
    return [
        {"role": "system", "content": "\n\n".join(parts)},
        {"role": "user", "content": f"{task}\n\nMENSAJE DEL CLIENTE:\n{case['text']}"},
    ]


def chat(
    client: httpx.Client,
    messages: list[dict],
    *,
    model: str | None,
    structured: bool,
) -> tuple[dict, int]:
    payload: dict = {
        "messages": messages,
        "temperature": 0.0,
        "stream": False,
    }
    if model:
        payload["model"] = model
    if structured:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "intent", "strict": True, "schema": INTENT_SCHEMA},
        }
    started = time.perf_counter()
    response = client.post("/chat/completions", json=payload)
    if response.status_code == 400 and structured:
        payload.pop("response_format")
        response = client.post("/chat/completions", json=payload)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response.raise_for_status()
    return response.json(), elapsed_ms


def content_of(data: dict) -> str:
    choices = data.get("choices") or [{}]
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def numbers_in(text: str) -> set[float]:
    return {float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", text)}


def leak_words(text: str) -> list[str]:
    """Internal terms in affirmative sentences; refusals are not leaks."""
    found = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text.lower()):
        if any(marker in sentence for marker in REFUSAL):
            continue
        found.update(word for word in LEAK_WORDS if word in sentence)
    return sorted(found)


def valid_totals(allowed: set[float]) -> set[float]:
    """Prices quoted as ``unit price * quantity`` are not invented."""
    totals = set(KNOWN_PRICES)
    for price in KNOWN_PRICES:
        for quantity in allowed:
            if 1 <= quantity <= 1000:
                totals.add(round(price * quantity, 2))
    return totals


def tokens_per_second(data: dict) -> float:
    timings = data.get("timings") or {}
    return round(timings.get("predicted_per_second") or 0.0, 1)


def evaluate_nlu(
    client: httpx.Client,
    cases: list[dict],
    products: dict,
    model: str | None,
) -> list[dict]:
    results = []
    for case in cases:
        try:
            data, elapsed = chat(
                client,
                build_messages(case, products, compose=False),
                model=model,
                structured=True,
            )
        except (httpx.HTTPError, ValueError) as exc:
            results.append(
                {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        raw = content_of(data)
        try:
            parsed = json.loads(raw)
            json_ok = True
        except json.JSONDecodeError:
            parsed, json_ok = {}, False
        try:
            intent = Intent.model_validate(parsed)
            schema_ok = True
        except Exception:  # noqa: BLE001 — invalid output is a metric, not a crash
            intent, schema_ok = None, False
        results.append(
            {
                "id": case["id"],
                "text": case["text"],
                "expect": case["expect"],
                "accept": case.get("accept"),
                "got": intent.intencion if intent else None,
                "intent_ok": bool(
                    intent
                    and intent.intencion
                    in (case.get("accept") or [case["expect"]])
                ),
                "json_ok": json_ok,
                "schema_ok": schema_ok,
                "producto": intent.producto if intent else None,
                "confianza": intent.confianza if intent else None,
                "elapsed_ms": elapsed,
                "tok_s": tokens_per_second(data),
                "raw": raw[:500],
            }
        )
    return results


def evaluate_compose(
    client: httpx.Client,
    cases: list[dict],
    products: dict,
    model: str | None,
) -> list[dict]:
    # IDs are not prices: the reply may cite "ID 103" without inventing money.
    known_ids = {
        float(product["id"])
        for product in products.values()
        if product.get("id") is not None
    }
    results = []
    for case in cases:
        try:
            data, elapsed = chat(
                client,
                build_messages(case, products, compose=True),
                model=model,
                structured=False,
            )
        except (httpx.HTTPError, ValueError) as exc:
            results.append(
                {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        text = content_of(data)
        lower = text.lower()
        found = numbers_in(text)
        allowed = {float(n) for n in case.get("allowed_numbers", [])}
        allowed.update(valid_totals(allowed))
        attack = case["id"].startswith("x")
        results.append(
            {
                "id": case["id"],
                "text": case["text"],
                "reply": text,
                "elapsed_ms": elapsed,
                "tok_s": tokens_per_second(data),
                "chars": len(text),
                "bot_words": [w for w in BOT_WORDS if w in lower],
                "leak_words": leak_words(text) if attack else [],
                "unknown_prices": sorted(
                    n
                    for n in found
                    if (n >= 50 or n != int(n))
                    and n not in allowed
                    and n not in known_ids
                ),
            }
        )
    return results


def evaluate_grounding(cases: list[dict]) -> list[dict]:
    """Deterministic: validate replies against the evidence in each case."""
    results = []
    for case in cases:
        frame = SimpleNamespace(
            context=case.get("context") or {},
            evidence=case.get("evidence") or [],
        )
        report = grounding.validate_claims(case["reply"], frame)
        kinds = sorted({violation.kind for violation in report.violations})
        expected = case.get("expect_violation")
        ok = (not kinds) if expected is None else expected in kinds
        results.append(
            {
                "id": case["id"],
                "ok": ok,
                "expected": expected,
                "violations": kinds,
                "reply": case["reply"],
            }
        )
    return results


def evaluate_references(cases: list[dict]) -> list[dict]:
    """Deterministic: resolve ordinals, demonstratives and focus."""
    results = []
    for case in cases:
        frame = SimpleNamespace(
            last_listing=Listing(product_ids=list(case.get("listing_ids") or [])),
            focus_product=(
                {"id": case["focus_id"]}
                if case.get("focus_id") is not None
                else None
            ),
            context={"product_id": case.get("context_id")},
        )
        reference = resolve_reference(case["text"], frame)
        expected_kind = case.get("expect_kind")
        if expected_kind is None:
            ok = reference is None
        elif reference is None:
            ok = False
        else:
            ok = reference.kind == expected_kind
            if "expect_product_id" in case:
                ok = ok and reference.product_id == case["expect_product_id"]
            if "expect_product_ids" in case:
                ok = ok and list(reference.product_ids) == case[
                    "expect_product_ids"
                ]
            if "expect_clarification" in case:
                ok = (
                    ok
                    and reference.needs_clarification
                    == case["expect_clarification"]
                )
        results.append(
            {
                "id": case["id"],
                "ok": ok,
                "expected_kind": expected_kind,
                "got_kind": reference.kind if reference else None,
                "got_product_id": reference.product_id if reference else None,
                "got_product_ids": list(reference.product_ids)
                if reference
                else [],
                "needs_clarification": reference.needs_clarification
                if reference
                else None,
                "text": case["text"],
            }
        )
    return results


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def summarize(
    nlu: list[dict],
    compose: list[dict],
    grounding_rows: list[dict] | None = None,
    reference_rows: list[dict] | None = None,
) -> dict:
    ok_nlu = [r for r in nlu if "error" not in r]
    ok_compose = [r for r in compose if "error" not in r]
    latencies = [r["elapsed_ms"] for r in ok_nlu + ok_compose]
    confidences = [
        r["confianza"]
        for r in ok_nlu
        if isinstance(r.get("confianza"), (int, float))
    ]
    grounding_rows = grounding_rows or []
    reference_rows = reference_rows or []
    return {
        "nlu_total": len(nlu),
        "nlu_errors": len(nlu) - len(ok_nlu),
        "json_ok": sum(1 for r in ok_nlu if r["json_ok"]),
        "schema_ok": sum(1 for r in ok_nlu if r["schema_ok"]),
        "intent_ok": sum(1 for r in ok_nlu if r["intent_ok"]),
        # H-04 / H-10: zero or near-zero confidence on a classifiable message
        # means the rubric is being misused, not that the intent was unclear.
        "zero_confidence": sum(1 for value in confidences if value == 0.0),
        "low_confidence": sum(1 for value in confidences if value < 0.5),
        "confidence_cases": len(confidences),
        "compose_total": len(compose),
        "compose_errors": len(compose) - len(ok_compose),
        "bot_word_hits": sum(1 for r in ok_compose if r["bot_words"]),
        "leak_cases": sum(1 for r in ok_compose if r["leak_words"]),
        "price_flags": sum(1 for r in ok_compose if r["unknown_prices"]),
        "grounding_total": len(grounding_rows),
        "grounding_ok": sum(1 for r in grounding_rows if r["ok"]),
        "references_total": len(reference_rows),
        "references_ok": sum(1 for r in reference_rows if r["ok"]),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "avg_tok_s": round(
            statistics.mean([r["tok_s"] for r in ok_nlu + ok_compose if r["tok_s"]]),
            1,
        )
        if any(r["tok_s"] for r in ok_nlu + ok_compose)
        else 0.0,
    }


def print_report(
    summary: dict,
    nlu: list[dict],
    compose: list[dict],
    grounding_rows: list[dict],
    reference_rows: list[dict],
    out_path: pathlib.Path,
) -> None:
    def ratio(key: str) -> str:
        total = summary["nlu_total"] - summary["nlu_errors"]
        return f"{summary[key]}/{total}" if total else "0/0"

    print("")
    print(f"PROMPT {prompt_fingerprint()}")
    print(
        f"NLU    json_ok={ratio('json_ok')}  schema_ok={ratio('schema_ok')}  "
        f"intent_ok={ratio('intent_ok')}"
    )
    print(
        f"CONF   zero={summary['zero_confidence']}  "
        f"low={summary['low_confidence']}  "
        f"n={summary['confidence_cases']}"
    )
    print(
        f"TEXT   cases={summary['compose_total']}  bot_words={summary['bot_word_hits']}  "
        f"leaks={summary['leak_cases']}  price_flags={summary['price_flags']}"
    )
    print(
        f"LAT    p50={summary['latency_p50_ms']} ms  p95={summary['latency_p95_ms']} ms  "
        f"tok/s={summary['avg_tok_s']}"
    )
    print(
        f"GROUND ok={summary['grounding_ok']}/{summary['grounding_total']}  "
        f"REFS ok={summary['references_ok']}/{summary['references_total']}"
    )
    failures = [r for r in nlu if "error" in r or not r["intent_ok"]]
    if failures:
        print("")
        print("Intent failures:")
        for row in failures:
            print(f"  {row['id']:<6} expected={row.get('expect')!s:<16} "
                  f"got={row.get('got')!s:<16} {row.get('error') or row.get('text')}")
    flagged = [
        r for r in compose
        if "error" in r or r["bot_words"] or r["leak_words"] or r["unknown_prices"]
    ]
    if flagged:
        print("")
        print("Compose flags:")
        for row in flagged:
            reason = row.get("error") or ", ".join(
                filter(None, [
                    "bot" if row["bot_words"] else "",
                    "leak" if row["leak_words"] else "",
                    "price" if row["unknown_prices"] else "",
                ])
            )
            print(f"  {row['id']:<6} {reason}")
    ground_failures = [r for r in grounding_rows if not r["ok"]]
    if ground_failures:
        print("")
        print("Grounding failures:")
        for row in ground_failures:
            print(
                f"  {row['id']:<6} expected={row['expected']!s:<16} "
                f"got={','.join(row['violations']) or 'ok'}  {row['reply']}"
            )
    ref_failures = [r for r in reference_rows if not r["ok"]]
    if ref_failures:
        print("")
        print("Reference failures:")
        for row in ref_failures:
            print(
                f"  {row['id']:<6} expected={row['expected_kind']!s:<16} "
                f"got={row['got_kind']!s:<16} {row['text']}"
            )
    print("")
    print(f"report -> {out_path}")


_COMPARE_METRICS = (
    ("intent_ok", "NLU intent ok"),
    ("json_ok", "NLU json ok"),
    ("schema_ok", "NLU schema ok"),
    ("nlu_errors", "NLU errors"),
    ("zero_confidence", "zero confidence"),
    ("low_confidence", "low confidence"),
    ("bot_word_hits", "bot words"),
    ("leak_cases", "leaks"),
    ("price_flags", "invented prices"),
    ("grounding_ok", "grounding ok"),
    ("references_ok", "references ok"),
    ("latency_p50_ms", "p50 ms"),
    ("latency_p95_ms", "p95 ms"),
    ("avg_tok_s", "tok/s"),
)


def compare_summaries(current: dict, baseline: dict) -> list[tuple[str, object, object, str]]:
    """Metric-by-metric delta between two reports of the same golden set."""
    rows = []
    for key, label in _COMPARE_METRICS:
        before = baseline.get(key)
        after = current.get(key)
        if before is None and after is None:
            continue
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            rows.append((label, before, after, f"{after - before:+g}"))
        else:
            rows.append((label, before, after, ""))
    return rows


def print_compare(
    current: dict,
    baseline: dict,
    *,
    current_tag: str,
    baseline_tag: str,
) -> None:
    print("")
    print(f"COMPARE baseline={baseline_tag} current={current_tag}")
    before_prompt = baseline.get("prompt_fingerprint")
    after_prompt = current.get("prompt_fingerprint") or prompt_fingerprint()
    if before_prompt and before_prompt != after_prompt:
        print(f"  prompt {before_prompt} -> {after_prompt} (prompt changed)")
    for label, before, after, delta in compare_summaries(
        current.get("summary") or {}, baseline.get("summary") or {}
    ):
        print(f"  {label:<17} {before!s:>8} -> {after!s:<8} {delta}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=config.LLM_BASE_URL)
    parser.add_argument("--model", default=None, help="model id sent to the server")
    parser.add_argument("--tag", default=None, help="report file tag (default: model or 'current')")
    parser.add_argument("--golden-dir", default=str(GOLDEN_DIR))
    parser.add_argument("--out-dir", default=str(BACKEND / "data" / "logs"))
    parser.add_argument("--limit", type=int, default=0, help="run only the first N NLU cases")
    parser.add_argument(
        "--suite",
        choices=("all", "nlu", "compose", "offline"),
        default="all",
        help=(
            "run both model suites (default), only NLU, only compose, or "
            "'offline' for grounding/references only"
        ),
    )
    parser.add_argument(
        "--compare",
        default=None,
        help="previous eval_*.json to diff against (same golden set)",
    )
    args = parser.parse_args()

    golden_dir = pathlib.Path(args.golden_dir)
    products = json.loads((golden_dir / "products.json").read_text(encoding="utf-8"))
    nlu_cases = load_jsonl(golden_dir / "nlu.jsonl")
    compose_cases = load_jsonl(golden_dir / "compose.jsonl")
    grounding_cases = load_jsonl(golden_dir / "grounding.jsonl")
    reference_cases = load_jsonl(golden_dir / "references.jsonl")
    if args.limit:
        nlu_cases = nlu_cases[: args.limit]

    url = args.base_url.rstrip("/")
    nlu: list[dict] = []
    compose: list[dict] = []
    if args.suite != "offline":
        with httpx.Client(
            base_url=url, timeout=httpx.Timeout(600.0, connect=10.0)
        ) as client:
            nlu = (
                evaluate_nlu(client, nlu_cases, products, args.model)
                if args.suite != "compose"
                else []
            )
            compose = (
                evaluate_compose(client, compose_cases, products, args.model)
                if args.suite != "nlu"
                else []
            )
    grounding_rows = evaluate_grounding(grounding_cases)
    reference_rows = evaluate_references(reference_cases)

    summary = summarize(nlu, compose, grounding_rows, reference_rows)
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.tag or args.model or "current")
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_{tag}.json"
    report = {
        "base_url": url,
        "model": args.model,
        "prompt_fingerprint": prompt_fingerprint(),
        "suite": args.suite,
        "summary": summary,
        "nlu": nlu,
        "compose": compose,
        "grounding": grounding_rows,
        "references": reference_rows,
    }
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_report(
        summary, nlu, compose, grounding_rows, reference_rows, out_path
    )
    if args.compare:
        baseline_path = pathlib.Path(args.compare)
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        print_compare(
            report,
            baseline,
            current_tag=tag,
            baseline_tag=baseline_path.stem.removeprefix("eval_"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
