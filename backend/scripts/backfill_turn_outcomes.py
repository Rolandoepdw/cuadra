"""Backfill turn outcomes from traces (migration 020, phase F0).

Before migration 020 the only record of what a turn understood was `traces`,
which is a debugging log with a 30-day retention. This script copies that
information into `turns` once, so the Conversations panel has history from day
one.

It is idempotent and only fills columns that are still NULL. A second run
reports nothing to do. Turns older than the oldest trace stay incomplete on
purpose: the data no longer exists and is not invented.

Usage (from ``backend/``):

    .venv\\Scripts\\python.exe scripts\\backfill_turn_outcomes.py --dry-run
    .venv\\Scripts\\python.exe scripts\\backfill_turn_outcomes.py
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from cuadra import config, db  # noqa: E402

_METHODS = (
    "pipeline._classify_light",
    "pipeline._resolve_product",
    "pipeline.handle_turn",
)

# Columns this script is allowed to write. Everything else is ignored.
_COLUMNS = {
    "intent",
    "product_id",
    "stage",
    "generator",
    "escalated",
    "sale_id",
    "latency_ms",
    "intent_ms",
    "compose_ms",
    "outcome",
    "error",
    "finished_at",
}


def _json(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _latest_traces(conn) -> dict[int, dict[str, dict]]:
    """Latest trace per (turn_id, method) for the methods that carry outcomes."""
    rows = conn.execute(
        """SELECT t.turn_id, t.method, t.output_json, t.ok, t.error,
                  t.duration_ms, t.timestamp
             FROM traces t
             JOIN (
                   SELECT turn_id, method, MAX(id) AS id
                     FROM traces
                    WHERE turn_id IS NOT NULL
                      AND method IN (?, ?, ?)
                    GROUP BY turn_id, method
                  ) latest ON latest.id = t.id""",
        _METHODS,
    ).fetchall()
    by_turn: dict[int, dict[str, dict]] = {}
    for row in rows:
        by_turn.setdefault(int(row["turn_id"]), {})[row["method"]] = dict(row)
    return by_turn


def _candidate_fields(traces: dict[str, dict]) -> dict:
    fields: dict = {}

    classify = traces.get("pipeline._classify_light")
    if classify is not None:
        output = _json(classify["output_json"])
        if output.get("intencion"):
            fields["intent"] = output["intencion"]

    resolve = traces.get("pipeline._resolve_product")
    if resolve is not None:
        output = _json(resolve["output_json"])
        if output.get("product_id") is not None:
            fields["product_id"] = output["product_id"]

    handle = traces.get("pipeline.handle_turn")
    if handle is not None:
        output = _json(handle["output_json"])
        for key in ("stage", "generator", "intent_ms", "compose_ms"):
            if output.get(key) is not None:
                fields[key] = output[key]
        if output.get("sale_id") is not None:
            fields["sale_id"] = output["sale_id"]
        if output.get("escalate"):
            fields["escalated"] = 1
        if handle.get("duration_ms") is not None:
            fields["latency_ms"] = int(handle["duration_ms"])
        if not handle.get("ok"):
            fields["outcome"] = "error"
            fields["error"] = handle.get("error")
        if handle.get("timestamp"):
            fields["finished_at"] = handle["timestamp"]

    return fields


def _apply(turn: dict, candidate: dict) -> dict:
    """Keep only values that add information to the current row."""
    update: dict = {}
    for key, value in candidate.items():
        if key not in _COLUMNS:
            continue
        if key in ("escalated", "outcome", "error"):
            # These have defaults, so only upgrade them when the trace proves
            # the turn escalated or failed.
            if value in (1, "error") and turn.get(key) != value:
                update[key] = value
            continue
        if turn.get(key) is None:
            update[key] = value
    return update


def backfill(conn, *, dry_run: bool = False) -> dict:
    turns = {
        int(row["id"]): dict(row)
        for row in conn.execute("SELECT * FROM turns").fetchall()
    }
    traces = _latest_traces(conn)
    updated = 0
    unchanged = 0
    now = datetime.now(timezone.utc).isoformat()

    for turn_id, by_method in traces.items():
        turn = turns.get(turn_id)
        if turn is None:
            continue
        update = _apply(turn, _candidate_fields(by_method))
        if not update:
            unchanged += 1
            continue
        updated += 1
        if dry_run:
            continue
        assignments = ", ".join(f"{key} = ?" for key in update)
        conn.execute(
            f"UPDATE turns SET {assignments}, updated_at = ? WHERE id = ?",
            (*update.values(), now, turn_id),
        )

    if not dry_run:
        conn.commit()

    return {
        "turns_total": len(turns),
        "turns_with_traces": len(traces),
        "turns_updated": updated,
        "turns_unchanged": unchanged,
        "turns_without_traces": len(turns) - len(traces),
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    args = parser.parse_args()

    conn = db.connect()
    try:
        applied = db.apply_migrations(conn)
        if applied:
            print(f"migraciones aplicadas: {', '.join(applied)}")
        print(f"base de datos: {config.DB_PATH}")
        report = backfill(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}turnos totales:        {report['turns_total']}")
    print(f"{prefix}turnos con trazas:     {report['turns_with_traces']}")
    print(f"{prefix}turnos actualizados:   {report['turns_updated']}")
    print(f"{prefix}turnos sin cambios:    {report['turns_unchanged']}")
    print(f"{prefix}turnos sin trazas:     {report['turns_without_traces']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
