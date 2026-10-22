"""Channel adapters. Telegram now; WhatsApp in Phase 2.

Each adapter module exposes the async methods of `base.Channel`
(`send_text/send_image/send_audio/typing`) plus `start()/stop()/mode()` and the
inbound wiring. Adapters are imported lazily so the package stays import-light.
"""


def get_adapter(channel: str, identity: str | None = None):
    """Return the adapter module for a channel and identity."""
    if channel == "telegram":
        return get_sender(identity or "bot")
    return None


def get_sender(sender: str):
    """Return the outbound adapter for a sender identity.

    `bot` = Bot API (aiogram); `userbot` = Telethon userbot.
    """
    if sender == "userbot":
        from cuadra.channels import telegram_user

        return telegram_user
    if sender == "bot":
        from cuadra.channels import telegram

        return telegram
    return None

