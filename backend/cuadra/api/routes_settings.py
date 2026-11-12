"""Dashboard-editable publication strategy (no code changes needed)."""

from fastapi import APIRouter, Depends, HTTPException, status

from cuadra import db
from cuadra.api.auth import require_pin
from cuadra.api.models import (
    BroadcastStrategyIn,
    DeliverySettingsIn,
    FollowupSettingsIn,
    SalesSettingsIn,
)
from cuadra.domain import broadcasts, events, sales, settings

router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_pin)])


def _public(values: dict[str, int]) -> dict[str, int]:
    return {
        "max_posts_per_day": values[settings.BROADCAST_DEFAULT_MAX_POSTS],
        "min_interval_s": values[settings.BROADCAST_DEFAULT_MIN_INTERVAL],
        "max_interval_s": values[settings.BROADCAST_DEFAULT_MAX_INTERVAL],
        "repeat_passes": values[settings.BROADCAST_REPEAT_PASSES],
        "repeat_window_days": values[settings.BROADCAST_REPEAT_WINDOW_DAYS],
    }


@router.get("/broadcast")
def get_broadcast_strategy() -> dict:
    conn = db.connect()
    try:
        return _public(settings.broadcast_defaults(conn))
    finally:
        conn.close()


@router.put("/broadcast")
def set_broadcast_strategy(
    payload: BroadcastStrategyIn, apply_to_groups: bool = False
) -> dict:
    data = {
        settings.BROADCAST_DEFAULT_MAX_POSTS: payload.max_posts_per_day,
        settings.BROADCAST_DEFAULT_MIN_INTERVAL: payload.min_interval_s,
        settings.BROADCAST_DEFAULT_MAX_INTERVAL: payload.max_interval_s,
        settings.BROADCAST_REPEAT_PASSES: payload.repeat_passes,
        settings.BROADCAST_REPEAT_WINDOW_DAYS: payload.repeat_window_days,
    }
    conn = db.connect()
    try:
        if apply_to_groups:
            result = broadcasts.apply_strategy_to_all(conn, data)
            return {**_public(result), "groups": result["groups"]}
        values = settings.set_broadcast_defaults(conn, data)
        events.log(
            conn,
            "dashboard",
            "broadcast_strategy_saved",
            entity="settings",
            details={"values": _public(values)},
        )
        return _public(values)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    finally:
        conn.close()


@router.get("/delivery")
def get_delivery_settings() -> dict:
    conn = db.connect()
    try:
        return {"transport_policy": settings.transport_policy(conn)}
    finally:
        conn.close()


@router.put("/delivery")
def set_delivery_settings(payload: DeliverySettingsIn) -> dict:
    conn = db.connect()
    try:
        value = settings.set_transport_policy(conn, payload.transport_policy)
        events.log(
            conn,
            "dashboard",
            "delivery_settings_saved",
            entity="settings",
            details={"transport_policy": value},
        )
        return {"transport_policy": value}
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    finally:
        conn.close()


@router.get("/sales")
def get_sales_settings() -> dict:
    conn = db.connect()
    try:
        return {"mode": sales.sale_mode(conn)}
    finally:
        conn.close()


@router.put("/sales")
def set_sales_settings(payload: SalesSettingsIn) -> dict:
    conn = db.connect()
    try:
        value = sales.set_sale_mode(conn, payload.mode)
        events.log(
            conn,
            "dashboard",
            "sales_settings_saved",
            entity="settings",
            details={"mode": value},
        )
        return {"mode": value}
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    finally:
        conn.close()


@router.get("/sales/followup")
def get_followup_settings() -> dict:
    conn = db.connect()
    try:
        return {"mode": settings.sales_followup_mode(conn)}
    finally:
        conn.close()


@router.put("/sales/followup")
def set_followup_settings(payload: FollowupSettingsIn) -> dict:
    conn = db.connect()
    try:
        value = settings.set_sales_followup_mode(conn, payload.mode)
        events.log(
            conn,
            "dashboard",
            "sale_followup_mode_saved",
            entity="settings",
            details={"mode": value},
        )
        return {"mode": value}
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    finally:
        conn.close()
