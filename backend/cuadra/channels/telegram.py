"""Telegram channel (aiogram 3, polling). No token => dummy mode.

Inbound messages (text/voice/audio/photo) are normalized and pushed into the
pipeline via ai/ingest.py. Outbound replies are delivered by the queue's
`send_message` job, which applies human-like delays before calling the
`send_*` methods below.
"""

import logging
import uuid
from pathlib import Path

from cuadra import config
from cuadra.ai import ingest

log = logging.getLogger("cuadra.telegram")

_bot = None
_dp = None
_polling = None


def available() -> bool:
    return _bot is not None


async def start() -> None:
    global _bot, _dp, _polling
    if not config.TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN empty — channel in dummy mode (development)")
        return
    from aiogram import Bot, Dispatcher, F

    _bot = Bot(config.TELEGRAM_TOKEN)
    _dp = Dispatcher()

    @_dp.message(F.text)
    async def on_text(message) -> None:
        await _handle(
            _bot,
            identifier=str(message.chat.id),
            from_user=message.from_user,
            type="text",
            body=message.text,
        )

    @_dp.message(F.voice | F.audio)
    async def on_audio(message) -> None:
        media = message.voice or message.audio
        suffix = Path(getattr(media, "file_name", None) or "voice.ogg").suffix or ".ogg"
        path = config.AUDIOS_DIR / f"{uuid.uuid4().hex}{suffix}"
        await _bot.download(media, destination=path)
        await _handle(
            _bot,
            identifier=str(message.chat.id),
            from_user=message.from_user,
            type="audio",
            file_path=f"audios/{path.name}",
        )

    @_dp.message(F.photo)
    async def on_photo(message) -> None:
        photo = message.photo[-1]
        path = config.IMAGES_DIR / f"{uuid.uuid4().hex}.jpg"
        await _bot.download(photo, destination=path)
        await _handle(
            _bot,
            identifier=str(message.chat.id),
            from_user=message.from_user,
            type="image",
            file_path=f"images/{path.name}",
        )

    _polling = _dp.start_polling(_bot, handle_signals=False)
    log.info("telegram polling started")


async def _handle(bot, *, identifier, from_user, type, body=None, file_path=None) -> None:
    name = from_user.full_name if from_user is not None else None
    try:
        ingest.ingest_incoming(
            channel="telegram",
            identifier=identifier,
            type=type,
            body=body,
            file_path=file_path,
            customer_name=name,
        )
    except Exception:  # noqa: BLE001 — never kill polling on a single bad message
        log.exception("failed to ingest incoming telegram message")


async def stop() -> None:
    global _bot, _dp, _polling
    if _polling is not None:
        _polling.cancel()
        _polling = None
    if _bot is not None:
        await _bot.session.close()
        _bot = None
    _dp = None
    log.info("telegram channel stopped")


async def send_text(target: str, text: str) -> None:
    if _bot is None:
        log.info("[dummy] send_text to %s: %s", target, text)
        return
    await _bot.send_message(target, text)


async def send_image(target: str, path: str, caption: str | None = None) -> None:
    if _bot is None:
        log.info("[dummy] send_image to %s: %s", target, path)
        return
    from aiogram.types import FSInputFile

    await _bot.send_photo(target, FSInputFile(path), caption=caption)


async def send_audio(target: str, path: str) -> None:
    if _bot is None:
        log.info("[dummy] send_audio to %s: %s", target, path)
        return
    from aiogram.types import FSInputFile

    await _bot.send_voice(target, FSInputFile(path))


async def typing(target: str) -> None:
    if _bot is None:
        return
    from aiogram.enums import ChatAction

    try:
        await _bot.send_chat_action(target, ChatAction.TYPING)
    except Exception:  # noqa: BLE001 — typing indicator is cosmetic
        log.debug("typing indicator failed for %s", target)


def mode() -> str:
    return "polling" if config.TELEGRAM_TOKEN else "dummy"
