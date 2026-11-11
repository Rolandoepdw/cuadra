"""H-06: p95 latency report from real telemetry (``llm_calls``).

Reads the configured database in read-only mode and groups successful model
calls by Havana hour, by purpose and by whether the call came after an idle
gap. That last split is the H-06 question: is the first call after inactivity
slower than a warm one?

Usage (from ``backend/``):

    .venv\\Scripts\\python.exe scripts\\latency_report.py
    .venv\\Scripts\\python.exe scripts\\latency_report.py --days 7 --purpose compose
    .venv\\Scripts\\python.exe scripts\\latency_report.py --all-purposes --gap 30

Percentiles use the nearest-rank method so a small sample is never smoothed
into a nicer number. Calls with an error or without latency are counted but do
not enter the percentile.
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from cuadra import config  # noqa: E402
from cuadra.domain.clock import HAVANA  # noqa: E402

_UTC_FMT = "%Y-%m-%d %H:%M:%S"


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100); None when there is no sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-p * len(ordered) // 100))))
    return ordered[rank - 1]


def _local(moment: str) -> datetime:
    utc = datetime.strptime(moment, _UTC_FMT).replace(tzinfo=timezone.utc)
    return utc.astimezone(HAVANA)


def _stats(rows: list[dict]) -> dict:
    latencies = [
        row["latency_ms"] for row in rows if row["ok"] and row["latency_ms"] is not None
    ]
    return {
        "calls": len(rows),
        "errors": sum(1 for row in rows if not row["ok"]),
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "max": max(latencies) if latencies else None,
    }


def analyze(rows: list[dict], gap_minutes: float = 30) -> dict:
    """Split telemetry into overall, per-purpose, per-hour and idle/warm stats."""
    ordered = sorted(rows, key=lambda row: row["created_at"])
    by_hour: dict[int, list[dict]] = {}
    by_purpose: dict[str, list[dict]] = {}
    idle: list[dict] = []
    warm: list[dict] = []
    previous: datetime | None = None
    for row in ordered:
        moment = _local(row["created_at"])
        by_hour.setdefault(moment.hour, []).append(row)
        by_purpose.setdefault(row["purpose"], []).append(row)
        if previous is None or (moment - previous) >= timedelta(
            minutes=gap_minutes
        ):
            idle.append(row)
        else:
            warm.append(row)
        previous = moment
    return {
        "overall": _stats(ordered),
        "by_purpose": {key: _stats(value) for key, value in sorted(by_purpose.items())},
        "by_hour": {hour: _stats(by_hour[hour]) for hour in sorted(by_hour)},
        "after_idle": _stats(idle),
        "warm": _stats(warm),
        "gap_minutes": gap_minutes,
    }


def fetch(days: float, purpose: str | None) -> list[dict]:
    uri = f"file:{config.DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = """SELECT created_at, purpose, latency_ms, ok
                 FROM llm_calls
                 WHERE created_at >= datetime('now', ?)"""
        args: list = [f"-{days} days"]
        if purpose:
            sql += " AND purpose = ?"
            args.append(purpose)
        rows = conn.execute(sql + " ORDER BY created_at", args)
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _line(label: str, stats: dict) -> str:
    def ms(value):
        return "—" if value is None else f"{value} ms"

    return (
        f"{label:<22} {stats['calls']:>5} {stats['errors']:>6} "
        f"{ms(stats['p50']):>9} {ms(stats['p95']):>9} {ms(stats['max']):>9}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="p95 latency by hour and idle gap (H-06)")
    parser.add_argument("--days", type=float, default=7, help="window in days (default 7)")
    parser.add_argument("--purpose", default="compose", help="purpose filter (default compose)")
    parser.add_argument("--all-purposes", action="store_true", help="ignore --purpose")
    parser.add_argument("--gap", type=float, default=30, help="idle gap in minutes (default 30)")
    args = parser.parse_args()

    purpose = None if args.all_purposes else args.purpose
    rows = fetch(args.days, purpose)
    if not rows:
        print(f"No hay llamadas en los últimos {args.days:g} días ({purpose or 'todos'}).")
        return 0

    report = analyze(rows, gap_minutes=args.gap)
    header = f"{'grupo':<22} {'n':>5} {'errores':>6} {'p50':>9} {'p95':>9} {'máx':>9}"
    print(f"Ventana: últimos {args.days:g} días · propósito: {purpose or 'todos'}")
    print("Horas en America/Havana; percentiles nearest-rank.\n")
    print(header)
    print("-" * len(header))
    print(_line("global", report["overall"]))
    print("\nPor propósito")
    for name, stats in report["by_purpose"].items():
        print(_line(name, stats))
    print("\nPor hora local")
    for hour, stats in report["by_hour"].items():
        print(_line(f"{hour:02d}:00", stats))
    print("\nEfecto de la inactividad")
    print(_line("tras inactividad", report["after_idle"]))
    print(_line("en caliente", report["warm"]))
    print(f"\nUmbral de inactividad: {args.gap:g} min.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
