"""Telegram channel (aiogram 3, polling). No token => dummy mode.

Inbound messages (text/voice/audio/photo) are normalized and pushed into the
pipeline via ai/ingest.py. Outbound replies are delivered by the queue's
`send_message` job, which applies human-like delays before calling the
`send_*` methods below.
"""

import asyncio
import logging
import uuid
from pathlib import Path

from cuadra import config
from cuadra.ai import ingest

log = logging.getLogger("cuadra.telegram")

_bot = None
_dp = None
_polling: "asyncio.Task | None" = None


def available() -> bool:
    return _bot is not None


async def start() -> None:
    global _bot, _dp, _polling
    if not config.TELEGRAM_ENABLED or not config.TELEGRAM_TOKEN:
        log.warning("telegram disabled or token empty - channel in dummy mode")
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
            external_id=str(message.message_id),
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
            external_id=str(message.message_id),
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
            external_id=str(message.message_id),
        )

    # start_polling is a long-running coroutine: it MUST be scheduled as a task,
    # otherwise the loop never actually long-polls and updates pile up in Telegram.
    _polling = asyncio.create_task(_dp.start_polling(_bot, handle_signals=False))
    _polling.add_done_callback(_on_polling_done)
    log.info("telegram polling started")


def _on_polling_done(task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("telegram polling stopped unexpectedly: %r", exc)


async def _handle(
    bot, *, identifier, from_user, type, body=None, file_path=None, external_id=None
) -> None:
    name = from_user.full_name if from_user is not None else None
    log.info("inbound telegram %s from %s (%s): %s", type, identifier, name, (body or file_path))
    try:
        ingest.ingest_incoming(
            channel="telegram",
            identifier=identifier,
            type=type,
            body=body,
            file_path=file_path,
            customer_name=name,
            handle=getattr(from_user, "username", None),
            identity="bot",
            external_id=external_id,
        )
    except Exception:  # noqa: BLE001 — never kill polling on a single bad message
        log.exception("failed to ingest incoming telegram message")


async def stop() -> None:
    global _bot, _dp, _polling
    if _polling is not None:
        _polling.cancel()
        try:
            await _polling
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — surface why polling died
            log.exception("telegram polling task ended with an error")
        _polling = None
    if _bot is not None:
        await _bot.session.close()
        _bot = None
    _dp = None
    log.info("telegram channel stopped")


async def send_text(target: str, text: str) -> str | None:
    if _bot is None:
        log.info("[dummy] send_text to %s: %s", target, text)
        return None
    message = await _send(lambda: _bot.send_message(target, text))
    message_id = getattr(message, "message_id", None)
    return str(message_id) if message_id is not None else None


async def send_image(target: str, path: str, caption: str | None = None) -> None:
    if _bot is None:
        log.info("[dummy] send_image to %s: %s", target, path)
        return
    from aiogram.types import FSInputFile

    await _send(lambda: _bot.send_photo(target, FSInputFile(path), caption=caption))


async def send_audio(target: str, path: str) -> None:
    if _bot is None:
        log.info("[dummy] send_audio to %s: %s", target, path)
        return
    from aiogram.types import FSInputFile

    await _send(lambda: _bot.send_voice(target, FSInputFile(path)))


async def _send(call):
    """Run a Telegram send, mapping permanent failures to PermanentJobError.

    A 400 (chat not found, bot blocked, user deactivated) never succeeds by
    retrying, so the queue must not back off forever on it. Network/timeouts
    stay transient and let the queue retry. Returns the Telegram object so the
    caller can persist its message id (sale dispatch correlation).
    """
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramNetworkError,
        TelegramRetryAfter,
        TelegramServerError,
    )

    from cuadra.ai.queue import PermanentJobError

    try:
        return await call()
    except TelegramRetryAfter as exc:
        raise TimeoutError(f"telegram retry after {exc.retry_after}s") from exc
    except (TelegramNetworkError, TelegramServerError) as exc:
        raise TimeoutError(str(exc)) from exc
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        raise PermanentJobError(str(exc)) from exc


async def typing(target: str) -> None:
    if _bot is None:
        return
    from aiogram.enums import ChatAction

    try:
        await _bot.send_chat_action(target, ChatAction.TYPING)
    except Exception:  # noqa: BLE001 — typing indicator is cosmetic
        log.debug("typing indicator failed for %s", target)


def mode() -> str:
    return "polling" if config.TELEGRAM_ENABLED and config.TELEGRAM_TOKEN else "dummy"

