"""Create the Telethon userbot session (interactive, run once).

Stop cuadra-core first: the SQLite session file cannot be opened by two
processes. Fill CUADRA_TELETHON_API_ID, CUADRA_TELETHON_API_HASH and optionally
CUADRA_TELETHON_PHONE in backend/.env, then run from backend/:

    .venv\\Scripts\\python.exe scripts\\telegram_login.py

Telegram will ask for the login code and, if enabled, the 2FA password.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuadra import config  # noqa: E402
from cuadra.channels import telegram_user  # noqa: E402


def main() -> int:
    if not config.TELETHON_API_ID or not config.TELETHON_API_HASH:
        print("Faltan CUADRA_TELETHON_API_ID / CUADRA_TELETHON_API_HASH en backend/.env")
        print("Crealos en https://my.telegram.org -> API development tools.")
        return 1

    phone = config.TELETHON_PHONE
    if not phone:
        phone = input("Telefono de la cuenta (ej. +5355555555): ").strip()

    return asyncio.run(_login(phone))


async def _login(phone: str) -> int:
    from telethon import TelegramClient

    client = TelegramClient(
        str(config.TELETHON_SESSION),
        config.TELETHON_API_ID,
        config.TELETHON_API_HASH,
    )
    try:
        await client.start(phone=phone)
        me = await client.get_me()
        print(f"Sesion creada: @{me.username or '?'} (id {me.id})")
        print(f"Archivo: {telegram_user.session_file()}")
    except sqlite3.OperationalError as exc:
        print(f"No se pudo abrir la sesion: {exc}")
        print("Deten cuadra-core antes de ejecutar este script (la sesion esta en uso).")
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
