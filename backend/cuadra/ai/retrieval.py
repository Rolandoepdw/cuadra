"""Hybrid product retrieval: FTS5 (BM25) + optional FastEmbed + RRF.

The lexical layer always works (SQLite FTS5 is part of the stdlib build used by
Cuadra). The semantic layer is optional: if `fastembed` is not installed or the
vendored model is missing, retrieval silently degrades to FTS5 + the fuzzy
matcher in `domain/catalog.py`. A missing model never blocks startup.

Embedding vectors are stored as float32 little-endian in `product_embeddings`;
`text_hash` avoids recomputing a product whose indexed text did not change.
"""

import array
import hashlib
import importlib.util
import json
import logging
import math
import re
import sqlite3
import sys
import threading

from cuadra import config
from cuadra.domain import catalog

log = logging.getLogger("cuadra.retrieval")

SEARCH_STATUSES = ("ACTIVE", "OUT_OF_STOCK")
_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "para",
    "con", "que", "por", "me", "mi", "te", "se", "es", "en", "al", "a",
    "lo", "su", "sus", "the", "of",
}

_model = None
_model_failed = False
_model_lock = threading.Lock()

try:  # optional: fastembed already depends on numpy, but do not require it
    import numpy as _np
except ImportError:  # pragma: no cover - exercised only on stripped installs
    _np = None


# ------------------------------------------------------------------- vectors

def _model_cached() -> bool:
    try:
        return any(
            path.suffix.lower() == ".onnx"
            for path in config.EMBEDDING_CACHE_DIR.rglob("*.onnx")
        )
    except OSError:
        return False


def embeddings_available() -> bool:
    """True only when the library and a vendored model are both present.

    Does not import fastembed nor download anything; safe to call at startup.
    """
    return importlib.util.find_spec("fastembed") is not None and _model_cached()


def _get_model():
    global _model, _model_failed
    if _model is not None:
        return _model
    if _model_failed or not embeddings_available():
        return None
    with _model_lock:
        if _model is not None:
            return _model
        if _model_failed:
            return None
        try:
            from fastembed import TextEmbedding

            _model = TextEmbedding(
                model_name=config.EMBEDDING_MODEL,
                cache_dir=str(config.EMBEDDING_CACHE_DIR),
                threads=config.EMBEDDING_THREADS,
                providers=["CPUExecutionProvider"],
            )
            log.info("embedding model ready: %s", config.EMBEDDING_MODEL)
        except Exception as exc:  # noqa: BLE001 - degraded mode is a feature
            _model_failed = True
            log.warning("embeddings disabled (%s); using fuzzy search", exc)
            return None
    return _model


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    model = _get_model()
    if model is None:
        return None
    try:
        return [[float(value) for value in vector] for vector in model.embed(texts)]
    except Exception as exc:  # noqa: BLE001 - never break a customer reply
        global _model_failed
        _model_failed = True
        log.warning("embedding inference failed (%s); using fuzzy search", exc)
        return None


def embed_text(text: str) -> list[float] | None:
    vectors = embed_texts([text])
    return vectors[0] if vectors else None


def _pack(vector: list[float]) -> bytes:
    values = array.array("f", vector)
    if sys.byteorder == "big":  # the on-disk format is explicitly little-endian
        values.byteswap()
    return values.tobytes()


def _unpack(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _cosine(query: list[float], vector: array.array) -> float:
    if len(vector) != len(query):
        return 0.0
    if _np is not None:
        q = _np.asarray(query, dtype=_np.float32)
        v = _np.frombuffer(vector.tobytes(), dtype="<f4")
        denominator = float(_np.linalg.norm(q) * _np.linalg.norm(v))
        if denominator == 0:
            return 0.0
        return float(_np.dot(q, v) / denominator)
    dot = sum(a * b for a, b in zip(query, vector))
    q_norm = math.sqrt(sum(value * value for value in query))
    v_norm = math.sqrt(sum(value * value for value in vector))
    if not q_norm or not v_norm:
        return 0.0
    return dot / (q_norm * v_norm)


# --------------------------------------------------------------------- index

def product_text(conn, product: dict) -> str:
    """Text that represents a product for FTS/embeddings (real columns only)."""
    category = ""
    if product.get("category_id"):
        row = conn.execute(
            "SELECT name FROM categories WHERE id = ?", (product["category_id"],)
        ).fetchone()
        category = row["name"] if row is not None else ""
    try:
        specs = json.loads(product.get("specifications_json") or "{}")
    except (TypeError, ValueError):
        specs = {}
    spec_text = " ".join(
        f"{key} {value}" for key, value in specs.items() if value is not None
    )
    return " ".join(
        part
        for part in (
            product.get("name") or "",
            product.get("description") or "",
            category,
            spec_text,
        )
        if str(part).strip()
    )


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_product(conn, product_id: int) -> bool:
    """Recompute one product embedding when its indexed text changed.

    Returns True when a new vector was written, False when it was already up to
    date, the product is gone, or embeddings are unavailable.
    """
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        return False
    product = dict(row)
    if product["status"] == "REMOVED":
        conn.execute("DELETE FROM product_embeddings WHERE product_id = ?", (product_id,))
        conn.commit()
        return False

    text = product_text(conn, product)
    digest = _text_hash(text)
    existing = conn.execute(
        "SELECT model, dim, text_hash FROM product_embeddings WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    if (
        existing is not None
        and existing["model"] == config.EMBEDDING_MODEL
        and existing["text_hash"] == digest
    ):
        return False

    vector = embed_text(text)
    if vector is None:
        return False
    conn.execute(
        """INSERT INTO product_embeddings
           (product_id, model, dim, vector, text_hash, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(product_id) DO UPDATE SET
               model = excluded.model,
               dim = excluded.dim,
               vector = excluded.vector,
               text_hash = excluded.text_hash,
               updated_at = datetime('now')""",
        (product_id, config.EMBEDDING_MODEL, len(vector), _pack(vector), digest),
    )
    conn.commit()
    return True


def reindex_all(conn, *, batch_size: int = 64) -> dict:
    """Recompute every missing/stale embedding in batches (low-priority job)."""
    if _get_model() is None:
        return {
            "available": False,
            "candidates": 0,
            "indexed": 0,
            "skipped": 0,
            "model": config.EMBEDDING_MODEL,
        }

    rows = conn.execute(
        "SELECT * FROM products WHERE status != 'REMOVED' ORDER BY id"
    ).fetchall()
    existing = {
        int(row["product_id"]): row["text_hash"]
        for row in conn.execute(
            "SELECT product_id, text_hash FROM product_embeddings WHERE model = ?",
            (config.EMBEDDING_MODEL,),
        )
    }
    pending: list[tuple[int, str, str]] = []
    skipped = 0
    for row in rows:
        product = dict(row)
        text = product_text(conn, product)
        digest = _text_hash(text)
        if existing.get(int(product["id"])) == digest:
            skipped += 1
            continue
        pending.append((int(product["id"]), text, digest))

    indexed = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = embed_texts([text for _, text, _ in batch])
        if vectors is None:
            break
        conn.executemany(
            """INSERT INTO product_embeddings
               (product_id, model, dim, vector, text_hash, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(product_id) DO UPDATE SET
                   model = excluded.model,
                   dim = excluded.dim,
                   vector = excluded.vector,
                   text_hash = excluded.text_hash,
                   updated_at = datetime('now')""",
            [
                (product_id, config.EMBEDDING_MODEL, len(vector), _pack(vector), digest)
                for (product_id, _text, digest), vector in zip(batch, vectors)
            ],
        )
        conn.commit()
        indexed += len(batch)
    return {
        "available": True,
        "candidates": len(pending),
        "indexed": indexed,
        "skipped": skipped,
        "model": config.EMBEDDING_MODEL,
    }


def needs_reindex(conn=None) -> bool:
    """True when at least one live product has no embedding for the active model."""
    own = conn is None
    if own:
        from cuadra import db

        conn = db.connect()
    try:
        row = conn.execute(
            """SELECT 1 FROM products p
               LEFT JOIN product_embeddings e
                      ON e.product_id = p.id AND e.model = ?
               WHERE p.status != 'REMOVED' AND e.product_id IS NULL LIMIT 1""",
            (config.EMBEDDING_MODEL,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        if own:
            conn.close()


def queue_reindex(product_id: int) -> int:
    """Enqueue a single-product reindex (used after catalog mutations)."""
    from cuadra.ai import queue

    return queue.enqueue(
        "reindex_product",
        {"product_id": int(product_id)},
        priority=config.PRIORITY_MAINTENANCE,
    )


def queue_reindex_all() -> int:
    from cuadra.ai import queue

    return queue.enqueue(
        "reindex_all_products", {}, priority=config.PRIORITY_MAINTENANCE
    )


# -------------------------------------------------------------------- search

def _fold(text: str) -> str:
    return catalog._fold(text)  # shared accent-folding rule


def _query_terms(text: str) -> list[str]:
    cleaned = "".join(
        char if char.isalnum() else " " for char in _fold(text or "").lower()
    )
    terms = [
        term
        for term in cleaned.split()
        if len(term) > 1 and term not in _STOPWORDS
    ]
    # Spanish plurals are common in customer messages ("cargadores", "cables")
    # while product names are usually singular. Adding the stem as an extra OR
    # term (never replacing the original) recovers those matches without a real
    # stemmer or extra dependency.
    expanded: list[str] = []
    for term in terms:
        if term not in expanded:
            expanded.append(term)
        for stem in _stems(term):
            if stem not in expanded:
                expanded.append(stem)
    return expanded


def _stems(term: str) -> list[str]:
    """Plural candidates (both ``-s`` and ``-es``), harmless as extra OR terms."""
    stems = []
    if len(term) > 3 and term.endswith("s"):
        stems.append(term[:-1])
    if len(term) > 4 and term.endswith("es"):
        stems.append(term[:-2])
    return [stem for stem in stems if stem != term]


def _fts_search(conn, query: str, *, limit: int, statuses=SEARCH_STATUSES) -> list[int]:
    terms = _query_terms(query)
    if not terms:
        return []
    match = " OR ".join(f'"{term}"' for term in terms)
    placeholders = ", ".join("?" for _ in statuses)
    try:
        rows = conn.execute(
            f"""SELECT f.product_id
                FROM products_fts f
                JOIN products p ON p.id = f.product_id
                WHERE products_fts MATCH ?
                  AND p.status IN ({placeholders})
                ORDER BY bm25(products_fts, 0.0, 4.0, 2.0, 2.0, 1.0)
                LIMIT ?""",
            (match, *statuses, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("FTS unavailable (%s); falling back to fuzzy", exc)
        return []
    return [int(row["product_id"]) for row in rows]


def _vector_search(conn, query: str, *, limit: int, statuses=SEARCH_STATUSES) -> list[int]:
    if _get_model() is None:
        return []
    vector = embed_text(query)
    if vector is None:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    try:
        rows = conn.execute(
            f"""SELECT e.product_id, e.vector, e.dim
                FROM product_embeddings e
                JOIN products p ON p.id = e.product_id
                WHERE e.model = ? AND p.status IN ({placeholders})""",
            (config.EMBEDDING_MODEL, *statuses),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    scored = []
    for row in rows:
        values = _unpack(row["vector"])
        if len(values) != int(row["dim"]):
            continue
        scored.append((_cosine(vector, values), int(row["product_id"])))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [product_id for _, product_id in scored[:limit]]


def reciprocal_rank_fusion(
    *rankings: list[int], k: int = 60
) -> list[tuple[int, float]]:
    """RRF: score(d) = sum(1 / (k + rank)) across every ranked list."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, product_id in enumerate(ranking, start=1):
            scores[int(product_id)] = scores.get(int(product_id), 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _ranked(conn, query: str, *, top_k: int, statuses=SEARCH_STATUSES):
    lexical = _fts_search(
        conn, query, limit=config.RETRIEVAL_CANDIDATES, statuses=statuses
    )
    semantic = _vector_search(
        conn, query, limit=config.RETRIEVAL_CANDIDATES, statuses=statuses
    )
    return reciprocal_rank_fusion(lexical, semantic, k=config.RRF_K)[:top_k]


def search_ids(
    conn, query: str, *, top_k: int | None = None, statuses=SEARCH_STATUSES
) -> list[int]:
    """Best product ids in rank order, with fuzzy fallback when both layers fail."""
    top_k = config.RETRIEVAL_TOP_K if top_k is None else top_k
    query = (query or "").strip()
    if not query:
        return []
    ranked = _ranked(conn, query, top_k=top_k, statuses=statuses)
    if ranked:
        return [product_id for product_id, _ in ranked]
    return [
        int(product["id"])
        for product in catalog.search_products(
            conn, query, limit=top_k, statuses=statuses
        )
    ]


def search(conn, query: str, *, top_k: int | None = None) -> list[dict]:
    """Customer-safe JSON for the top products, preserving RRF order."""
    top_k = config.RETRIEVAL_TOP_K if top_k is None else top_k
    ranked = _ranked(conn, query, top_k=top_k)
    if not ranked:
        return [
            {
                **catalog.llm_product_json(conn, product),
                "match_score": product.get("match_score", 0.0),
            }
            for product in catalog.search_products(
                conn, query, limit=top_k, statuses=SEARCH_STATUSES
            )
        ]

    best = ranked[0][1] or 1.0
    results: list[dict] = []
    for product_id, score in ranked:
        try:
            product = catalog.get_product(conn, product_id)
        except catalog.NotFound:
            continue
        if product["status"] not in SEARCH_STATUSES:
            continue
        results.append(
            {
                **catalog.llm_product_json(conn, product),
                "match_score": round(score / best, 3),
            }
        )
    return results
