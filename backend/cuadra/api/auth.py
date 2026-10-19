"""Dashboard authentication via PIN (X-PIN header)."""

import secrets

from fastapi import Header, HTTPException, status

from cuadra import config


async def require_pin(x_pin: str = Header(default="")) -> None:
    """Dependency for /api/* routes. With an empty PIN in config, allows all."""
    if not config.PIN:
        return
    if not secrets.compare_digest(x_pin, config.PIN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid PIN")
