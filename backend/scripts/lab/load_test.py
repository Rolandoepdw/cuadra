"""Load test for Cuadra's pipeline (Fase 0).

Simulates N customers following a fixed script. Each message goes through
the real ingest + pipeline path (not the HTTP endpoint), so we measure the
true end-to-end latency of the turn processor without network or queue
overhead.

This is a *sequential* load test: one customer at a time, one message at a
time. With llama.cpp running ``-np 1`` (single slot), concurrent LLM calls
would serialize anyway, so sequential execution gives the same per-message
latency without risking timeouts.

Usage (from ``backend/``):

    .venv\\Scripts\\python.exe scripts\\lab\\load_test.py

Outputs a JSON report and a human-readable summary with p50/p95 latencies,
sales per TCP, and per-stage timing breakdowns (intent, compose).

Requires: the local LLM server running on the configured endpoint.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

from cuadra import config, db
from cuadra.ai import ingest, pipeline
from cuadra.domain import conversations, turns

# ---------------------------------------------------------------------------
# Script: one scene per TCP supplier. Each scene is a list of
# (customer_text, expect_sale) steps.
# ---------------------------------------------------------------------------

SCRIPTS = {
    "Roger": [
        ("Hola", False),
        ("Me interesa la pizza personal", False),
        ("Ok quiero 5", False),
        ("Esto es Marianao", False),
        ("Calle 51 #4410", False),
        ("Mi telefono es 55545678", False),
        ("Si, quiero confirmar el pedido", True),
    ],
    "Yoan": [
        ("Hola", False),
        ("Tienes cascos para moto?", False),
        ("Cuanto valen?", False),
        ("Ok me llevo uno", True),
        ("Esta en Marianao", False),
        ("Direccion Calle 20 #123", False),
        ("Telefono 71234567", False),
        ("Si confirmo", True),
    ],
    "Rolando": [
        ("Buenas", False),
        ("Necesito una tablet android", False),
        ("Cual es el precio?", False),
        ("Me la llevo", True),
        ("Entrego en Marianao", False),
        ("Direccion Calle 10 #456", False),
        ("Telefono 55523456", False),
        ("Confirmo", True),
    ],
    "Claudia": [
        ("Hola", False),
        ("Tienen microondas?", False),
        ("Cuanto cuesta el de 20 litros?", False),
        ("Ok quiero uno", True),
        ("Vivo en Diez de Octubre", False),
        ("Calle 15 #789", False),
        ("Mi telefono es 55567801", False),
        ("Confirmo el pedido", True),
    ],
}


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-p * len(ordered) // 100))))
    return ordered[rank - 1]


def stats(label: str, values: list[float]) -> dict:
    return {
        "label": label,
        "n": len(values),
        "p50": round(percentile(values, 50), 1) if values else None,
        "p95": round(percentile(values, 95), 1) if values else None,
        "max": round(max(values), 1) if values else None,
        "min": round(min(values), 1) if values else None,
    }


async def process_one_turn(turn_id: int, conn) -> dict:
    turn = turns.get(conn, turn_id)
    conversation = conversations.get_conversation(conn, turn["conversation_id"])
    started = time.perf_counter()
    result = await pipeline.handle_turn(conn, conversation, turn)
    total_ms = int((time.perf_counter() - started) * 1000)
    return {
        "turn_id": turn_id,
        "conversation_id": turn["conversation_id"],
        "total_ms": total_ms,
        "intent_ms": result.get("intent_ms", 0),
        "compose_ms": result.get("compose_ms", 0),
        "generator": result.get("generator"),
        "intent": result.get("intent", {}).get("intencion"),
        "sale_id": result.get("sale_id"),
        "sale_status": result.get("sale_status"),
    }


async def run_scenario(supplier_name: str, script: list[tuple[str, bool]], conn) -> dict:
    row = conn.execute(
        "SELECT id, name FROM suppliers WHERE name = ?", (supplier_name,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"Supplier '{supplier_name}' not found in DB")
    supplier_id = int(row["id"])

    identifier = f"loadtest-{supplier_name.lower()}-{int(time.time()*1000)}"

    info = ingest.ingest_incoming(
        channel="telegram",
        identifier=identifier,
        type="text",
        body=script[0][0],
        customer_name=f"Cliente {supplier_name}",
        identity="bot",
        external_id=f"lt-{supplier_name}-0",
    )
    conversation_id = info["conversation_id"]
    turn_id = info["turn_id"]

    step_results = []
    sale_created = False

    for idx, (text, expect_sale) in enumerate(script):
        if idx > 0:
            info = ingest.ingest_incoming(
                channel="telegram",
                identifier=identifier,
                type="text",
                body=text,
                customer_name=f"Cliente {supplier_name}",
                identity="bot",
                external_id=f"lt-{supplier_name}-{idx}",
            )
            turn_id = info["turn_id"]

        r = await process_one_turn(turn_id, conn)
        step_results.append(
            {
                "step": idx,
                "text": text,
                "expect_sale": expect_sale,
                "total_ms": r["total_ms"],
                "intent_ms": r["intent_ms"],
                "compose_ms": r["compose_ms"],
                "generator": r["generator"],
                "intent": r["intent"],
                "sale_id": r["sale_id"],
                "sale_status": r["sale_status"],
            }
        )
        if r["sale_id"] is not None:
            sale_created = True

        # Brief pause between customer messages
        await asyncio.sleep(0.3)

    sales = conn.execute(
        """SELECT COUNT(*) FROM sales
           WHERE supplier_id = ? AND conversation_id = ?""",
        (supplier_id, conversation_id),
    ).fetchone()[0]

    return {
        "supplier": supplier_name,
        "supplier_id": supplier_id,
        "conversation_id": conversation_id,
        "steps": step_results,
        "sales_created": sales,
        "sale_created": sale_created,
    }


async def main_async(concurrency: int = 4) -> dict:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row

    suppliers = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM suppliers WHERE active = 1 ORDER BY id LIMIT ?",
            (concurrency,),
        )
    ]

    if not suppliers:
        raise SystemExit("No active suppliers found in DB")

    print(f"Load test: {len(suppliers)} customers (sequential)")
    for s in suppliers:
        print(f"  - {s}: {len(SCRIPTS.get(s, []))} steps")

    started = time.perf_counter()
    results = []
    for s in suppliers:
        script = SCRIPTS.get(s, [("Hola", False)])
        print(f"  Running {s}...")
        r = await run_scenario(s, script, conn)
        results.append(r)

    wall_ms = int((time.perf_counter() - started) * 1000)
    conn.close()

    all_latencies = []
    all_intent = []
    all_compose = []
    total_sales = 0
    suppliers_with_sale = 0

    for r in results:
        total_sales += r["sales_created"]
        if r["sale_created"]:
            suppliers_with_sale += 1
        for s in r["steps"]:
            all_latencies.append(s["total_ms"])
            all_intent.append(s["intent_ms"])
            all_compose.append(s["compose_ms"])

    report = {
        "wall_clock_ms": wall_ms,
        "customers": len(results),
        "suppliers": [r["supplier"] for r in results],
        "total_sales": total_sales,
        "suppliers_with_sale": suppliers_with_sale,
        "per_supplier": [
            {
                "supplier": r["supplier"],
                "conversation_id": r["conversation_id"],
                "sales_created": r["sales_created"],
                "steps": len(r["steps"]),
                "avg_latency_ms": round(
                    sum(s["total_ms"] for s in r["steps"]) / len(r["steps"]), 1
                )
                if r["steps"]
                else None,
            }
            for r in results
        ],
        "latency_overall": stats("inbound->outbound total", all_latencies),
        "intent_ms": stats("intent (LLM)", all_intent),
        "compose_ms": stats("compose (LLM/template)", all_compose),
        "steps": [s for r in results for s in r["steps"]],
    }

    return report


def print_report(report: dict) -> None:
    print("\n" + "=" * 60)
    print("LOAD TEST REPORT (Fase 0 - sequential pipeline baseline)")
    print("=" * 60)
    print(f"Wall clock:        {report['wall_clock_ms']} ms")
    print(f"Customers:         {report['customers']}")
    print(f"Total sales:       {report['total_sales']}")
    print(f"Suppliers w/ sale: {report['suppliers_with_sale']}/{report['customers']}")
    print()
    print("Per supplier:")
    for p in report["per_supplier"]:
        print(
            f"  {p['supplier']:<12} sales={p['sales_created']}  "
            f"steps={p['steps']}  avg={p['avg_latency_ms']} ms"
        )
    print()
    print("Latency breakdown:")
    for key in ("latency_overall", "intent_ms", "compose_ms"):
        s = report[key]
        print(
            f"  {s['label']:<30} n={s['n']:<3} p50={s['p50']:>6} ms  "
            f"p95={s['p95']:>6} ms  max={s['max']:>6} ms  min={s['min']:>6} ms"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Cuadra pipeline load test")
    parser.add_argument(
        "--concurrency", type=int, default=4, help="number of customers (suppliers)"
    )
    parser.add_argument(
        "--output", type=str, default=None, help="write JSON report to this file"
    )
    args = parser.parse_args()

    report = asyncio.run(main_async(concurrency=args.concurrency))
    print_report(report)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\nJSON report -> {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
