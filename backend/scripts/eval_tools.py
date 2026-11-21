"""Measure tool selection and argument accuracy on the golden set.

The planner needs the local LLM server (the same one the bot uses). Run from
`backend/`:

    .venv\\Scripts\\python.exe scripts\\eval_tools.py
    .venv\\Scripts\\python.exe scripts\\eval_tools.py --json

Metrics (doc 15 §10.4):
- tool_selection_accuracy: the first proposed tool matches `expected_tool`.
- tool_args_accuracy: the selection matches and every expected arg matches.
"""

import argparse
import asyncio
import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cuadra.ai import llm  # noqa: E402
from cuadra.ai.tools import base, catalog, escalations, memory, sales  # noqa: E402

GOLDEN = ROOT / "tests" / "golden" / "tools.jsonl"


def _registry() -> base.Registry:
    registry = base.Registry()
    catalog.register(registry)
    sales.register(registry)
    memory.register(registry)
    escalations.register(registry)
    return registry


def _normalize(key: str, value):
    if not isinstance(value, str):
        return value
    folded = unicodedata.normalize("NFKD", value.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c)).strip()
    # "laptops" and "laptop" are the same search for the tool.
    if key in ("query", "category", "zone") and folded.endswith("s"):
        folded = folded[:-1]
    return folded


def _args_match(actual: dict, expected: dict) -> bool:
    for key, value in expected.items():
        if key not in actual:
            return False
        if _normalize(key, actual[key]) != _normalize(key, value):
            return False
    return True


async def run() -> dict:
    rows = [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    registry = _registry()
    allowed = registry.allowed("on")
    results = []
    selection_ok = 0
    args_ok = 0
    for row in rows:
        messages = [
            *row.get("history", []),
            {"role": "user", "content": row["text"]},
        ]
        error = None
        try:
            calls = await llm.plan_tools(
                messages, allowed, context=row.get("context")
            )
        except Exception as exc:  # noqa: BLE001 - the report must include failures
            calls = []
            error = f"{type(exc).__name__}: {exc}"
        first = calls[0] if calls else None
        selected = first.name if first is not None else None
        accepted = row.get("accepted") or [
            {"tool": row["expected_tool"], "args": row["expected_args"]}
        ]
        selected_ok = any(selected == item["tool"] for item in accepted)
        selected_args_ok = any(
            selected == item["tool"] and _args_match(first.args, item["args"])
            for item in accepted
        )
        selection_ok += selected_ok
        args_ok += selected_args_ok
        results.append(
            {
                "id": row["id"],
                "text": row["text"],
                "expected_tool": row["expected_tool"],
                "expected_args": row["expected_args"],
                "selected_tool": selected,
                "selected_args": first.args if first is not None else None,
                "selection_ok": selected_ok,
                "args_ok": selected_args_ok,
                "must_not_call": row.get("must_not_call") or [],
                "error": error,
            }
        )
    total = len(rows) or 1
    return {
        "total": len(rows),
        "tool_selection_accuracy": round(100 * selection_ok / total, 1),
        "tool_args_accuracy": round(100 * args_ok / total, 1),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="JSON only")
    args = parser.parse_args()
    report = asyncio.run(run())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(
        f"tool_selection_accuracy = {report['tool_selection_accuracy']} % "
        f"({report['total']} casos)"
    )
    print(f"tool_args_accuracy      = {report['tool_args_accuracy']} %")
    for row in report["results"]:
        mark = "OK " if row["selection_ok"] and row["args_ok"] else "FAIL"
        print(
            f"  [{mark}] {row['id']}: esperado {row['expected_tool']}, "
            f"elegido {row['selected_tool']}"
        )
        if row["error"]:
            print(f"         error: {row['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
