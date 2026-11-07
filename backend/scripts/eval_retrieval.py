"""Phase 5: offline retrieval evaluation (recall@k) on the synthetic golden set.

The golden set already labels every NLU turn with the product it is about, so it
can double as a retrieval benchmark: query = customer message, expected result =
the labelled product. This runs the real retrieval layer (FTS5 + fuzzy, plus the
optional embeddings when they are vendored) against a throwaway SQLite database.

It never reads the real database, never calls the model server and never sends
Telegram messages.

Usage (from ``backend/``):

    .venv\\Scripts\\python.exe scripts\\eval_retrieval.py
    .venv\\Scripts\\python.exe scripts\\eval_retrieval.py --top-k 3 --tag golden
"""

import argparse
import json
import pathlib
import sys
import tempfile

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from cuadra import config, db  # noqa: E402
from cuadra.ai import retrieval  # noqa: E402

GOLDEN_DIR = BACKEND / "tests" / "golden"


def load_jsonl(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def seed(conn, products: dict) -> dict[str, int]:
    conn.execute("INSERT INTO suppliers (id, name, delivery_zone) VALUES (1, 'TCP demo', 'La Habana')")
    ids: dict[str, int] = {}
    for key, product in products.items():
        ids[key] = int(product["id"])
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, name, description, price, currency, commission,
                stock, status, specifications_json)
               VALUES (?, 1, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                product["id"],
                product["nombre"],
                product.get("descripcion"),
                product["precio"],
                product["moneda"],
                product.get("stock") or 0,
                product.get("estado") or "ACTIVE",
                json.dumps(product.get("especificaciones") or {}, ensure_ascii=False),
            ),
        )
    conn.commit()
    return ids


def _has_signal(text: str, products: dict) -> bool:
    """True when the message mentions a product name (cold retrieval is fair)."""
    folded = retrieval._fold(text or "").lower()
    for product in products.values():
        for word in retrieval._fold(product["nombre"]).lower().split():
            if len(word) > 3 and word in folded:
                return True
    return False


def evaluate(
    conn, cases: list[dict], ids: dict[str, int], products: dict, top_k: int
) -> list[dict]:
    rows = []
    for case in cases:
        expected_key = case.get("product")
        if not expected_key:
            continue
        expected = ids[expected_key]
        ranking = retrieval.search_ids(conn, case["text"], top_k=top_k)
        rows.append(
            {
                "id": case["id"],
                "text": case["text"],
                "expect": expected_key,
                "expect_id": expected,
                "ranking": ranking,
                "has_signal": _has_signal(case["text"], products),
                "hit@1": bool(ranking) and ranking[0] == expected,
                "hit@k": expected in ranking,
            }
        )
    return rows


def _recall(rows: list[dict], top_k: int) -> dict:
    total = len(rows)
    hit1 = sum(1 for row in rows if row["hit@1"])
    hitk = sum(1 for row in rows if row["hit@k"])
    return {
        "cases": total,
        f"recall@1": round(100 * hit1 / total, 1) if total else 0.0,
        f"recall@{top_k}": round(100 * hitk / total, 1) if total else 0.0,
        "misses": [row["id"] for row in rows if not row["hit@k"]],
    }


def summarize(rows: list[dict], top_k: int) -> dict:
    summary = _recall(rows, top_k)
    # Queries that never mention a product ("lo quiero", "cuanto vale eso")
    # depend on conversation context, not cold retrieval. Report them apart so
    # the target is not hidden by an unfair denominator.
    summary["with_signal"] = _recall(
        [row for row in rows if row["has_signal"]], top_k
    )
    summary["context_only"] = _recall(
        [row for row in rows if not row["has_signal"]], top_k
    )
    return summary


def print_report(summary: dict, rows: list[dict], out_path: pathlib.Path, top_k: int) -> None:
    print("")
    print(
        f"RETRIEVAL cases={summary['cases']}  recall@1={summary['recall@1']}%  "
        f"recall@{top_k}={summary[f'recall@{top_k}']}%"
    )
    print(
        f"  with product signal: {summary['with_signal'][f'recall@{top_k}']}% "
        f"({summary['with_signal']['cases']} cases)  |  "
        f"context only: {summary['context_only'][f'recall@{top_k}']}% "
        f"({summary['context_only']['cases']} cases)"
    )
    if summary["misses"]:
        print("Misses:")
        for row in rows:
            if row["hit@k"]:
                continue
            flag = "signal" if row["has_signal"] else "context"
            print(
                f"  {row['id']:<5} [{flag:<7}] expect={row['expect']:<10} "
                f"got={row['ranking']}  {row['text']}"
            )
    print("")
    print(f"report -> {out_path}")


def main() -> int:
    top_k = 3
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-dir", default=str(GOLDEN_DIR))
    parser.add_argument("--out-dir", default=str(BACKEND / "data" / "logs"))
    parser.add_argument("--top-k", type=int, default=top_k)
    parser.add_argument("--tag", default="golden")
    args = parser.parse_args()

    golden_dir = pathlib.Path(args.golden_dir)
    products = json.loads((golden_dir / "products.json").read_text(encoding="utf-8"))
    cases = load_jsonl(golden_dir / "nlu.jsonl")

    old_db_path = config.DB_PATH
    with tempfile.TemporaryDirectory(prefix="cuadra-retrieval-") as tmp:
        config.DB_PATH = pathlib.Path(tmp) / "eval.db"
        try:
            conn = db.connect()
            try:
                db.apply_migrations(conn)
                ids = seed(conn, products)
                rows = evaluate(conn, cases, ids, products, args.top_k)
            finally:
                conn.close()
        finally:
            config.DB_PATH = old_db_path

    summary = summarize(rows, args.top_k)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_retrieval_{args.tag}.json"
    report = {
        "embeddings": retrieval.embeddings_available(),
        "top_k": args.top_k,
        "summary": summary,
        "cases": rows,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(summary, rows, out_path, args.top_k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
