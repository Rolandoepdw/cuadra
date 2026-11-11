"""Backfill the numeric Telegram id of every supplier stored by handle only.

`suppliers.telegram_id` is a mutable `@usuario`: the owner can rename it or
release it, and whoever takes it next would receive business traffic. The
numeric id (`suppliers.telegram_peer_id`) is stable, so inbound TCP messages can
always be correlated back to the right row.

Run it once after registering suppliers by hand, or any time an operator adds a
TCP without writing the numeric id in the dashboard. Stop cuadra-core first: the
Telethon session file cannot be opened by two processes.

Usage (from `backend/`):

    .venv\\Scripts\\python.exe scripts\\resolve_supplier_peers.py --dry-run
    .venv\\Scripts\\python.exe scripts\\resolve_supplier_peers.py

The dry run only lists who is missing an id; it never connects to Telegram.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuadra import config, db  # noqa: E402
from cuadra.channels import telegram_user  # noqa: E402
from cuadra.domain import catalog, events  # noqa: E402


def _pending() -> list[dict]:
    conn = db.connect()
    try:
        return catalog.list_suppliers_missing_peer(conn)
    finally:
        conn.close()


def _store(supplier_id: int, handle: str, peer: str) -> None:
    conn = db.connect()
    try:
        catalog.set_supplier_telegram_peer(conn, supplier_id, peer)
        events.log(
            conn,
            "system",
            "supplier_peer_resolved",
            entity="suppliers",
            entity_id=supplier_id,
            details={"telegram_id": handle, "peer_id": peer, "source": "backfill"},
        )
    finally:
        conn.close()


async def _run(dry_run: bool) -> int:
    pending = _pending()
    if not pending:
        print("Todos los TCP activos ya tienen su id numérico de Telegram.")
        return 0

    print(f"TCP activos sin id numérico: {len(pending)}")
    for supplier in pending:
        print(f"  #{supplier['id']} {supplier['name']} -> {supplier['telegram_id']}")

    if dry_run:
        print("Dry run: no se contactó a Telegram. Quita --dry-run para resolverlos.")
        return 0

    if not config.TELETHON_API_ID or not config.TELETHON_API_HASH:
        print("Faltan CUADRA_TELETHON_API_ID / CUADRA_TELETHON_API_HASH en backend/.env")
        return 1

    await telegram_user.start()
    if not telegram_user.available():
        print(f"No se pudo iniciar el userbot: {telegram_user.status().get('error')}")
        return 1

    failures = 0
    try:
        for index, supplier in enumerate(pending):
            handle = supplier["telegram_id"]
            try:
                peer = await telegram_user.resolve_peer(handle)
            except Exception as exc:  # noqa: BLE001 - one bad handle must not stop the rest
                print(f"  ERROR {supplier['name']} ({handle}): {exc}")
                failures += 1
                peer = None
            if peer:
                _store(supplier["id"], handle, peer)
                print(f"  OK {supplier['name']} ({handle}) -> {peer}")
            elif peer is None and failures == 0:
                print(f"  SIN ID {supplier['name']} ({handle})")
            if index < len(pending) - 1:
                # Same account-wide pacing as any userbot send: resolving is a
                # Telegram call too and flood limits are shared.
                await asyncio.sleep(config.USERBOT_MIN_SEND_GAP_S)
    finally:
        await telegram_user.stop()

    print(f"Listo: {len(pending) - failures} resueltos, {failures} con error.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="solo listar los TCP sin id numérico (no contacta a Telegram)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
