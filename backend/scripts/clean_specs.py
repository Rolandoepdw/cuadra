"""Limpia especificaciones contaminadas por mensajes que no eran respuestas.

Prueba #3 (F2): una queja del TCP ("No me has dado los datos de la venta...")
se guardó como `dato_proveedor` del producto. Este script recorre
`products.specifications_json` y elimina los valores que `tcp_triage` reconoce
como queja o negativa de proceso, no como dato de producto.

Es idempotente y por defecto solo muestra lo que haría.

Usage:
    .venv\\Scripts\\python.exe scripts\\clean_specs.py
    .venv\\Scripts\\python.exe scripts\\clean_specs.py --apply
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuadra import db
from cuadra.ai import tcp_triage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="escribe los cambios (sin esta bandera solo reporta)",
    )
    args = parser.parse_args()

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, specifications_json FROM products "
            "WHERE specifications_json IS NOT NULL AND specifications_json != ''"
        ).fetchall()
        changed = 0
        for row in rows:
            try:
                specs = json.loads(row["specifications_json"] or "{}")
            except (TypeError, ValueError):
                print(f"#{row['id']} {row['name']}: JSON inválido, se omite")
                continue
            if not isinstance(specs, dict):
                continue
            clean = {
                key: value
                for key, value in specs.items()
                if not (isinstance(value, str) and not tcp_triage.looks_like_answer(value))
            }
            if clean == specs:
                continue
            changed += 1
            removed = [key for key in specs if key not in clean]
            print(f"#{row['id']} {row['name']}: quitar {', '.join(removed)}")
            if args.apply:
                conn.execute(
                    "UPDATE products SET specifications_json = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (
                        json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                        row["id"],
                    ),
                )
        if args.apply:
            conn.commit()
            print(f"{changed} producto(s) limpiados.")
        else:
            print(f"{changed} producto(s) por limpiar (usar --apply).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
