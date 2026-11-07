"""Common channel interface (Telegram now, WhatsApp in Phase 2)."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Channel(Protocol):
    async def send_text(self, target: str, text: str) -> str | None: ...

    async def send_image(self, target: str, path: str, caption: str | None) -> None: ...

    async def send_audio(self, target: str, path: str) -> None: ...

    async def typing(self, target: str) -> None: ...
