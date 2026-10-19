"""Telegram channel (aiogram, polling). No token => dummy mode for development.

Hito 1: start/stop and dummy mode only. Incoming-message handlers are wired
into the conversation pipeline in Hito 4.
"""

import logging

from cuadra import config

log = logging.getLogger("cuadra.telegram")

_bot = None
_dp = None
_polling = None


async def start() -> None:
    global _bot, _dp, _polling
    if not config.TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN empty — channel in dummy mode (development)")
        return
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    _bot = Bot(
        config.TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    _dp = Dispatcher()
    # Hito 4: register conversation pipeline handlers here.
    _polling = _dp.start_polling(_bot, handle_signals=False)
    log.info("telegram polling started")


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


def mode() -> str:
    return "polling" if config.TELEGRAM_TOKEN else "dummy"
