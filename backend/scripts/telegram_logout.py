"""Close and delete the Telethon userbot session (run with core stopped).

This both revokes the authorization on Telegram's side (log_out) and removes
the local SQLite session. Use it before switching to another Telegram account
or when you want to stop using the userbot entirely.

    .venv\\Scripts\\python.exe scripts\\telegram_logout.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuadra import config  # noqa: E402


def _session_path() -> Path:
    return Path(str(config.TELETHON_SESSION) + ".session")


def _remove_session_files() -> None:
    base = _session_path()
    for path in (base, Path(str(base) + "-journal")):
        if path.exists():
            path.unlink()
            print(f"Archivo eliminado: {path}")


async def _logout() -> int:
    from telethon import TelegramClient

    session = _session_path()
    if not session.exists():
        print("No hay sesion local que cerrar.")
        return 0
    if not config.TELETHON_API_ID or not config.TELETHON_API_HASH:
        print("Faltan CUADRA_TELETHON_API_ID / API_HASH: no puedo revocar la sesion en Telegram.")
        print(f"Borra manualmente {session} y termina la sesion desde Telegram > Dispositivos.")
        return 1

    client = TelegramClient(
        str(config.TELETHON_SESSION),
        config.TELETHON_API_ID,
        config.TELETHON_API_HASH,
    )
    await client.connect()
    try:
        if await client.is_user_authorized():
            await client.log_out()
            print("Sesion revocada en Telegram (log_out).")
        else:
            print("La sesion local ya no estaba autorizada.")
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    _remove_session_files()
    print("Sesion local eliminada. Deten el core antes de ejecutar este script.")
    return 0


def main() -> int:
    try:
        return asyncio.run(_logout())
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo cerrar la sesion: {exc}")
        print("Si el archivo esta en uso, deten cuadra-core y vuelve a intentarlo.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
