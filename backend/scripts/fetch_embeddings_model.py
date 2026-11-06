"""Download the optional multilingual embedding model exactly once.

The normal service startup NEVER downloads anything: `ai/retrieval.py` only
loads the model when an ONNX file already exists under
`backend/models/fastembed/`. Run this script once on a machine with internet:

    .venv\\Scripts\\python.exe scripts\\fetch_embeddings_model.py

Then copy `backend/models/fastembed/` to the offline laptop. If the folder is
missing, the bot keeps working with FTS5 + fuzzy search.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cuadra import config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=config.EMBEDDING_MODEL,
        help=f"FastEmbed model name (default: {config.EMBEDDING_MODEL})",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=config.EMBEDDING_THREADS,
        help="CPU inference threads",
    )
    args = parser.parse_args()

    try:
        from fastembed import TextEmbedding
    except ImportError:
        print(
            "fastembed is not installed.\n"
            "  .venv\\Scripts\\python.exe -m pip install fastembed",
            file=sys.stderr,
        )
        return 1

    # Always let FastEmbed resolve the exact model: a generic `.onnx` file in the
    # cache directory does not prove that this model was downloaded, and
    # FastEmbed is a no-op when the model is already cached.
    config.EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Preparing {args.model} in {config.EMBEDDING_CACHE_DIR} ...")
    try:
        model = TextEmbedding(
            model_name=args.model,
            cache_dir=str(config.EMBEDDING_CACHE_DIR),
            threads=max(1, args.threads),
            providers=["CPUExecutionProvider"],
        )
        vectors = list(model.embed(["prueba de modelo multilingüe"]))
    except Exception as exc:  # noqa: BLE001 - report the real reason to the operator
        print(f"Could not download/load the model: {exc}", file=sys.stderr)
        return 1

    dim = len(vectors[0]) if vectors else 0
    print(f"Model ready: {args.model} ({dim} dimensions)")
    print("Startup will now use it; no network access is required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
