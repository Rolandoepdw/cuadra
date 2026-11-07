"""Telegram userbot channel (Telethon).

Used ONLY for broadcast groups whose `sender` is `userbot`. Conversational flows
stay on the Bot API: running both on the same chats would duplicate replies.

The userbot is optional. It starts only when api_id/api_hash and a session file
exist; otherwise the core keeps running with the bot. Create the session once
with `python scripts/telegram_login.py` while cuadra-core is stopped.
"""

import asyncio
import logging
import re
import sqlite3
import uuid
from pathlib import Path

from cuadra import config, db
from cuadra.ai import ingest
from cuadra.channels import rate_limit

log = logging.getLogger("cuadra.telegram_user")

_USERBOT_SENDER = "userbot"
_entity_cache: dict[str, object] = {}

_client = None
_me = None
_status: dict = {
    "configured": False,
    "connected": False,
    "username": None,
    "user_id": None,
    "error": None,
}

_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)"
)
_NUMERIC_RE = re.compile(r"-?\d+")


def session_file() -> Path:
    path = Path(config.TELETHON_SESSION)
    return path if path.suffix == ".session" else Path(f"{path}.session")


def configured() -> bool:
    return bool(config.TELETHON_API_ID and config.TELETHON_API_HASH)


def available() -> bool:
    return _client is not None


def status() -> dict:
    return {
        **_status,
        "configured": configured(),
        "session_file": str(session_file()),
        "session_exists": session_file().exists(),
        "connected": _client is not None,
    }


def _set_error(message: str | None) -> None:
    _status["error"] = message
    _status["connected"] = _client is not None


def _sender_name(sender) -> str | None:
    parts = [
        getattr(sender, "first_name", None),
        getattr(sender, "last_name", None),
    ]
    name = " ".join(part for part in parts if part).strip()
    return name or getattr(sender, "username", None)


def _ingest_userbot(identifier: str, name: str | None, **kwargs) -> None:
    ingest.ingest_incoming(
        channel="telegram",
        identifier=identifier,
        customer_name=name,
        identity="userbot",
        **kwargs,
    )


async def _on_private_message(event) -> None:
    try:
        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return
        if getattr(sender, "id", None) == _status.get("user_id"):
            return
        message = event.message
        identifier = str(event.sender_id)
        name = _sender_name(sender)
        if message.text:
            log.info("inbound userbot text from %s (%s): %s", identifier, name, message.text)
            _ingest_userbot(
                identifier,
                name,
                type="text",
                body=message.text,
                external_id=str(message.id),
            )
        elif message.voice or message.audio:
            media = message.voice or message.audio
            suffix = Path(getattr(media, "file_name", None) or "").suffix
            if not suffix:
                suffix = ".ogg" if message.voice else ".mp3"
            config.AUDIOS_DIR.mkdir(parents=True, exist_ok=True)
            path = config.AUDIOS_DIR / f"{uuid.uuid4().hex}{suffix}"
            await message.download_media(file=str(path))
            log.info("inbound userbot audio from %s (%s): %s", identifier, name, path.name)
            _ingest_userbot(
                identifier,
                name,
                type="audio",
                file_path=f"audios/{path.name}",
                external_id=str(message.id),
            )
        elif message.photo:
            config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            path = config.IMAGES_DIR / f"{uuid.uuid4().hex}.jpg"
            await message.download_media(file=str(path))
            log.info("inbound userbot image from %s (%s): %s", identifier, name, path.name)
            _ingest_userbot(
                identifier,
                name,
                type="image",
                file_path=f"images/{path.name}",
                external_id=str(message.id),
            )
    except Exception:  # noqa: BLE001 - never let one bad update kill the userbot
        log.exception("failed to ingest incoming userbot message")


async def start() -> None:
    global _client, _me
    if _client is not None:
        return
    if not configured():
        log.info("userbot disabled (CUADRA_TELETHON_API_ID/API_HASH empty)")
        _set_error(None)
        return
    if not session_file().exists():
        log.warning(
            "userbot session missing at %s; run scripts/telegram_login.py",
            session_file(),
        )
        _set_error("session missing")
        return

    from telethon import TelegramClient, events

    client = TelegramClient(
        str(config.TELETHON_SESSION),
        config.TELETHON_API_ID,
        config.TELETHON_API_HASH,
        # Never let Telethon silently sleep on a flood wait: the queue needs to
        # see the error to park every userbot job for the same account.
        flood_sleep_threshold=0,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("userbot session is not authorized; run scripts/telegram_login.py")
            _set_error("session not authorized")
            await client.disconnect()
            return
        me = await client.get_me()
    except Exception as exc:  # noqa: BLE001 - never take the core down for the userbot
        log.exception("userbot failed to start")
        _set_error(str(exc))
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return

    _client = client
    _me = me
    _status["connected"] = True
    _status["username"] = getattr(me, "username", None)
    _status["user_id"] = getattr(me, "id", None)
    _status["error"] = None
    client.add_event_handler(
        _on_private_message,
        events.NewMessage(incoming=True, func=lambda event: event.is_private),
    )
    log.info(
        "userbot connected as @%s (id %s); listening for private messages",
        _status["username"] or "?",
        _status["user_id"],
    )


async def stop() -> None:
    global _client, _me
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            log.debug("userbot disconnect failed", exc_info=True)
        _client = None
    _me = None
    _entity_cache.clear()
    _status.update(
        {"connected": False, "username": None, "user_id": None}
    )
    log.info("userbot stopped")


def _require_client():
    if _client is None:
        from cuadra.ai.queue import PermanentJobError

        raise PermanentJobError(
            "userbot is not connected; check api_id/api_hash and run scripts/telegram_login.py"
        )
    return _client


def _lookup_peer(target: str) -> int | None:
    """Canonical Telethon peer id already stored for this target, if any."""
    raw = (target or "").strip()
    if not raw:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT peer_id FROM broadcast_groups
               WHERE target = ? AND peer_id IS NOT NULL LIMIT 1""",
            (raw,),
        ).fetchone()
        return int(row["peer_id"]) if row is not None else None
    except sqlite3.Error:
        # Migration 009 may not be applied yet; resolving still works.
        log.debug("peer_id lookup failed for %s", raw, exc_info=True)
        return None
    finally:
        conn.close()


def _remember_entity(target: str, entity) -> None:
    """Cache an entity in memory and persist its peer id for future sends.

    The second step is what stops `ImportChatInviteRequest` from running on
    every single message, which is what triggered Telegram's flood waits.
    """
    raw = (target or "").strip()
    if not raw:
        return
    _entity_cache[raw] = entity
    try:
        from telethon.utils import get_peer_id

        peer_id = int(get_peer_id(entity))
    except Exception:  # noqa: BLE001 - cache is best-effort
        log.debug("could not compute peer id for %s", raw, exc_info=True)
        return
    _entity_cache[str(peer_id)] = entity
    conn = db.connect()
    try:
        conn.execute(
            """UPDATE broadcast_groups SET peer_id = ?
               WHERE target = ? AND (peer_id IS NULL OR peer_id != ?)""",
            (peer_id, raw, peer_id),
        )
        conn.commit()
    except sqlite3.Error:
        log.debug("could not persist peer id for %s", raw, exc_info=True)
    finally:
        conn.close()


async def _resolve(client, target: str):
    """Resolve @username, numeric id or invite link to a Telethon entity.

    Resolution is cached because joining by invite link on every send is an
    account-level flood signal. A cached peer id is tried first; the invite is
    imported only when the group has never been resolved before.
    """
    raw = (target or "").strip()
    if not raw:
        from cuadra.ai.queue import PermanentJobError

        raise PermanentJobError("empty group target")

    cached = _entity_cache.get(raw)
    if cached is not None:
        return cached

    peer_id = _lookup_peer(raw)
    if peer_id is not None:
        cached = _entity_cache.get(str(peer_id))
        if cached is not None:
            _entity_cache[raw] = cached
            return cached
        try:
            entity = await client.get_entity(peer_id)
        except (ValueError, TypeError):
            log.debug("cached peer %s for %s is stale; resolving again", peer_id, raw)
        else:
            _remember_entity(raw, entity)
            return entity

    if _NUMERIC_RE.fullmatch(raw):
        entity = await client.get_entity(int(raw))
        _remember_entity(raw, entity)
        return entity

    invite = _INVITE_RE.search(raw)
    if invite is not None:
        entity = await _resolve_invite(client, invite.group(1))
        _remember_entity(raw, entity)
        return entity

    try:
        entity = await client.get_entity(raw)
    except ValueError:
        if raw.startswith("@"):
            raise
        entity = await client.get_entity(f"@{raw}")
    _remember_entity(raw, entity)
    return entity


async def _resolve_invite(client, invite_hash: str):
    from telethon.errors import (
        InviteHashExpiredError,
        InviteHashInvalidError,
        UserAlreadyParticipantError,
    )
    from telethon.tl.functions.messages import (
        CheckChatInviteRequest,
        ImportChatInviteRequest,
    )

    from cuadra.ai.queue import PermanentJobError

    try:
        info = await client(CheckChatInviteRequest(invite_hash))
    except (InviteHashExpiredError, InviteHashInvalidError) as exc:
        raise PermanentJobError(f"invite link is no longer valid: {exc}") from exc

    # ChatInviteAlready carries the chat when the account is a member: never
    # join again in that case, joining is the action Telegram watches most.
    chat = getattr(info, "chat", None)
    if chat is not None:
        return chat

    try:
        updates = await client(ImportChatInviteRequest(invite_hash))
        chats = getattr(updates, "chats", None) or []
        if chats:
            return chats[0]
    except UserAlreadyParticipantError:
        pass
    except (InviteHashExpiredError, InviteHashInvalidError) as exc:
        raise PermanentJobError(f"invite link is no longer valid: {exc}") from exc

    info = await client(CheckChatInviteRequest(invite_hash))
    chat = getattr(info, "chat", None)
    if chat is None:
        raise PermanentJobError("could not resolve invite link to a group")
    return chat


def _map_error(exc: Exception) -> Exception:
    from telethon.errors import (
        ChannelInvalidError,
        ChannelPrivateError,
        ChatAdminRequiredError,
        ChatWriteForbiddenError,
        FloodWaitError,
        InviteHashExpiredError,
        InviteHashInvalidError,
        PeerFloodError,
        SlowModeWaitError,
        UserBannedInChannelError,
    )

    from cuadra.ai.queue import PermanentJobError, RateLimited

    if isinstance(exc, FloodWaitError):
        seconds = getattr(exc, "seconds", 0) or 0
        return RateLimited(seconds, f"telegram rate limit: wait {seconds}s")
    if isinstance(exc, SlowModeWaitError):
        seconds = getattr(exc, "seconds", 0) or 0
        # Slow mode is per chat; the account itself is not in trouble. It is
        # a real Telegram wait, but not an account-wide punishment, so it is
        # parked without consuming the FloodWait hit budget.
        return RateLimited(
            seconds,
            f"telegram slow mode: wait {seconds}s",
            account=False,
            penalize=False,
        )
    if isinstance(exc, PeerFloodError):
        seconds = config.TELEGRAM_PEER_FLOOD_COOLDOWN_S
        return RateLimited(
            seconds, f"telegram peer flood limit: cooling down {seconds:.0f}s"
        )
    if isinstance(
        exc,
        (
            ChannelInvalidError,
            ChannelPrivateError,
            ChatAdminRequiredError,
            ChatWriteForbiddenError,
            InviteHashExpiredError,
            InviteHashInvalidError,
            UserBannedInChannelError,
            ValueError,
        ),
    ):
        return PermanentJobError(str(exc))
    return exc


def _guard_account() -> None:
    """Refuse to touch Telegram while the account is cooling down.

    This is a pre-flight guard: the real FloodWait was already counted when it
    happened, so the deferral itself does not consume the hit budget.
    """
    wait = rate_limit.cooldown_remaining(_USERBOT_SENDER)
    if wait > 0:
        from cuadra.ai.queue import RateLimited

        raise RateLimited(
            wait,
            f"telegram userbot cooldown: wait {wait:.0f}s",
            account=False,
            penalize=False,
        )


def _map_or_cool_down(exc: Exception) -> Exception:
    """Map a Telethon error and persist any account-wide punishment."""
    from cuadra.ai.queue import RateLimited

    mapped = _map_error(exc)
    if isinstance(mapped, RateLimited) and mapped.account:
        rate_limit.set_cooldown(
            _USERBOT_SENDER, mapped.retry_after, str(mapped)
        )
    return mapped


async def send_text(target: str, text: str) -> str | None:
    client = _require_client()
    _guard_account()
    try:
        entity = await _resolve(client, target)
        sent = await client.send_message(entity, text, link_preview=False)
    except Exception as exc:  # noqa: BLE001 - mapped below
        mapped = _map_or_cool_down(exc)
        if mapped is exc:
            raise
        raise mapped from exc
    rate_limit.mark_action(_USERBOT_SENDER)
    message_id = getattr(sent, "id", None)
    return str(message_id) if message_id is not None else None


async def send_image(target: str, path: str, caption: str | None = None) -> None:
    client = _require_client()
    abs_path = Path(path)
    if not abs_path.is_file():
        from cuadra.ai.queue import PermanentJobError

        raise PermanentJobError(f"image not found: {path}")
    _guard_account()
    try:
        entity = await _resolve(client, target)
        await client.send_file(
            entity,
            str(abs_path),
            caption=caption,
            force_document=False,
        )
    except Exception as exc:  # noqa: BLE001 - mapped below
        mapped = _map_or_cool_down(exc)
        if mapped is exc:
            raise
        raise mapped from exc
    rate_limit.mark_action(_USERBOT_SENDER)


async def send_audio(target: str, path: str) -> None:
    client = _require_client()
    abs_path = Path(path)
    if not abs_path.is_file():
        from cuadra.ai.queue import PermanentJobError

        raise PermanentJobError(f"audio not found: {path}")
    _guard_account()
    try:
        entity = await _resolve(client, target)
        await client.send_file(entity, str(abs_path), voice_note=True)
    except Exception as exc:  # noqa: BLE001 - mapped below
        mapped = _map_or_cool_down(exc)
        if mapped is exc:
            raise
        raise mapped from exc
    rate_limit.mark_action(_USERBOT_SENDER)


async def typing(target: str) -> None:
    client = _client
    if client is None:
        return
    if rate_limit.cooldown_remaining(_USERBOT_SENDER) > 0:
        # Cosmetic only; never touch Telegram during a cooldown.
        return
    try:
        entity = await _resolve(client, target)
        await client.action(entity, "typing")
        await asyncio.sleep(0)
    except Exception:  # noqa: BLE001 - cosmetic only
        log.debug("userbot typing failed for %s", target)


def mode() -> str:
    if _client is not None:
        return "userbot"
    if configured():
        return "userbot-offline"
    return "disabled"
