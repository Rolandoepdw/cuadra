"""Channel adapters. Telegram now; WhatsApp in Phase 2.

Each adapter module exposes the async methods of `base.Channel`
(`send_text/send_image/send_audio/typing`) plus `start()/stop()/mode()` and the
inbound wiring. Adapters are imported lazily so the package stays import-light.
"""


def get_adapter(channel: str):
    """Return the adapter module for a channel name, or None if unknown."""
    if channel == "telegram":
        from cuadra.channels import telegram

        return telegram
    return None
