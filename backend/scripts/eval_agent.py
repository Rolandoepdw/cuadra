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
import json
import pathlib
import re
import statistics
import sys
import time

import httpx

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from cuadra.ai import prompts  # noqa: E402
from cuadra.ai.llm import INTENT_SCHEMA, Intent  # noqa: E402
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
                "got": intent.intencion if intent else None,
                "intent_ok": bool(intent and intent.intencion == case["expect"]),
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
                    n for n in found if (n >= 50 or n != int(n)) and n not in allowed
                ),
            }
        )
    return results


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def summarize(nlu: list[dict], compose: list[dict]) -> dict:
    ok_nlu = [r for r in nlu if "error" not in r]
    ok_compose = [r for r in compose if "error" not in r]
    latencies = [r["elapsed_ms"] for r in ok_nlu + ok_compose]
    return {
        "nlu_total": len(nlu),
        "nlu_errors": len(nlu) - len(ok_nlu),
        "json_ok": sum(1 for r in ok_nlu if r["json_ok"]),
        "schema_ok": sum(1 for r in ok_nlu if r["schema_ok"]),
        "intent_ok": sum(1 for r in ok_nlu if r["intent_ok"]),
        "compose_total": len(compose),
        "compose_errors": len(compose) - len(ok_compose),
        "bot_word_hits": sum(1 for r in ok_compose if r["bot_words"]),
        "leak_cases": sum(1 for r in ok_compose if r["leak_words"]),
        "price_flags": sum(1 for r in ok_compose if r["unknown_prices"]),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "avg_tok_s": round(
            statistics.mean([r["tok_s"] for r in ok_nlu + ok_compose if r["tok_s"]]),
            1,
        )
        if any(r["tok_s"] for r in ok_nlu + ok_compose)
        else 0.0,
    }


def print_report(summary: dict, nlu: list[dict], compose: list[dict], out_path: pathlib.Path) -> None:
    def ratio(key: str) -> str:
        total = summary["nlu_total"] - summary["nlu_errors"]
        return f"{summary[key]}/{total}" if total else "0/0"

    print("")
    print(f"NLU    json_ok={ratio('json_ok')}  schema_ok={ratio('schema_ok')}  "
          f"intent_ok={ratio('intent_ok')}")
    print(
        f"TEXT   cases={summary['compose_total']}  bot_words={summary['bot_word_hits']}  "
        f"leaks={summary['leak_cases']}  price_flags={summary['price_flags']}"
    )
    print(
        f"LAT    p50={summary['latency_p50_ms']} ms  p95={summary['latency_p95_ms']} ms  "
        f"tok/s={summary['avg_tok_s']}"
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
    print("")
    print(f"report -> {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=config.LLM_BASE_URL)
    parser.add_argument("--model", default=None, help="model id sent to the server")
    parser.add_argument("--tag", default=None, help="report file tag (default: model or 'current')")
    parser.add_argument("--golden-dir", default=str(GOLDEN_DIR))
    parser.add_argument("--out-dir", default=str(BACKEND / "data" / "logs"))
    parser.add_argument("--limit", type=int, default=0, help="run only the first N NLU cases")
    args = parser.parse_args()

    golden_dir = pathlib.Path(args.golden_dir)
    products = json.loads((golden_dir / "products.json").read_text(encoding="utf-8"))
    nlu_cases = load_jsonl(golden_dir / "nlu.jsonl")
    compose_cases = load_jsonl(golden_dir / "compose.jsonl")
    if args.limit:
        nlu_cases = nlu_cases[: args.limit]

    url = args.base_url.rstrip("/")
    with httpx.Client(base_url=url, timeout=httpx.Timeout(600.0, connect=10.0)) as client:
        nlu = evaluate_nlu(client, nlu_cases, products, args.model)
        compose = evaluate_compose(client, compose_cases, products, args.model)

    summary = summarize(nlu, compose)
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.tag or args.model or "current")
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_{tag}.json"
    report = {
        "base_url": url,
        "model": args.model,
        "summary": summary,
        "nlu": nlu,
        "compose": compose,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(summary, nlu, compose, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
