"""Broadcast group and campaign endpoints (Hito 7)."""

from fastapi import APIRouter, Depends, HTTPException, status

from cuadra import db
from cuadra.api.auth import require_pin
from cuadra.api.models import (
    BroadcastGroupIn,
    BroadcastGroupPatch,
    BroadcastIn,
    BroadcastPatch,
)
from cuadra.domain import broadcasts
from cuadra.domain.errors import DomainError, NotFound

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])


def _http(exc: DomainError) -> HTTPException:
    code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, NotFound)
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(code, str(exc))


def _run(fn, *args, **kwargs):
    conn = db.connect()
    try:
        return fn(conn, *args, **kwargs)
    except DomainError as exc:
        raise _http(exc) from exc
    finally:
        conn.close()


# ------------------------------------------------------------------- groups


@router.get("/broadcast-groups")
def list_groups(channel: str | None = None, status_filter: str | None = None) -> list[dict]:
    return _run(broadcasts.list_groups, channel=channel, status=status_filter)


@router.post("/broadcast-groups", status_code=status.HTTP_201_CREATED)
def create_group(payload: BroadcastGroupIn) -> dict:
    return _run(broadcasts.create_group, payload.model_dump())


@router.get("/broadcast-groups/{group_id}")
def get_group(group_id: int) -> dict:
    return _run(broadcasts.get_group, group_id)


@router.patch("/broadcast-groups/{group_id}")
def update_group(group_id: int, payload: BroadcastGroupPatch) -> dict:
    return _run(broadcasts.update_group, group_id, payload.model_dump(exclude_unset=True))


@router.delete("/broadcast-groups/{group_id}")
def delete_group(group_id: int) -> dict:
    return _run(broadcasts.delete_group, group_id)


# --------------------------------------------------------------- campaigns


@router.get("/broadcasts")
def list_broadcasts(status_filter: str | None = None) -> list[dict]:
    return _run(broadcasts.list_broadcasts, status=status_filter)


@router.post("/broadcasts", status_code=status.HTTP_201_CREATED)
def create_broadcast(payload: BroadcastIn) -> dict:
    return _run(broadcasts.create_broadcast, payload.model_dump())


@router.get("/broadcasts/{broadcast_id}")
def get_broadcast(broadcast_id: int) -> dict:
    return _run(broadcasts.get_broadcast, broadcast_id)


@router.patch("/broadcasts/{broadcast_id}")
def update_broadcast(broadcast_id: int, payload: BroadcastPatch) -> dict:
    if payload.status == "CANCELLED":
        return _run(broadcasts.cancel_broadcast, broadcast_id)
    data = payload.model_dump(exclude_unset=True)
    data.pop("status", None)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to update")
    return _run(broadcasts.update_broadcast, broadcast_id, data)


@router.post("/broadcasts/reevaluate")
def reevaluate_broadcasts() -> dict:
    return _run(broadcasts.reevaluate_all)


@router.post("/broadcasts/{broadcast_id}/send-now")
def send_now(broadcast_id: int) -> dict:
    return _run(broadcasts.send_now, broadcast_id)


@router.delete("/broadcasts/{broadcast_id}")
def delete_broadcast(broadcast_id: int) -> dict:
    return _run(broadcasts.delete_broadcast, broadcast_id)


@router.get("/broadcasts/{broadcast_id}/posts")
def list_posts(broadcast_id: int) -> list[dict]:
    return _run(broadcasts.list_posts, broadcast_id)
