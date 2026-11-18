"""Customers panel endpoints (phase F3).

The list and detail mask phone/address by default. Revealing full PII is a
separate audited endpoint. Blocking, merging and signal review are explicit
operator actions; nothing here is triggered automatically by the bot.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from cuadra import db
from cuadra.api.auth import require_pin
from cuadra.api.models import (
    CustomerMergeIn,
    CustomerNoteIn,
    CustomerPatch,
    CustomerSettingsIn,
    CustomerTagIn,
)
from cuadra.domain import customer_panel
from cuadra.domain.errors import Invalid, NotFound

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])


def _run(fn, *args, **kwargs):
    conn = db.connect()
    try:
        return fn(conn, *args, **kwargs)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except Invalid as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    finally:
        conn.close()


# Fixed paths must be declared before `/{customer_id}`.
@router.get("/customers/summary")
def customers_summary() -> dict:
    return _run(customer_panel.summary)


@router.get("/customers/merge/preview")
def customers_merge_preview(
    from_id: int = Query(ge=1),
    into_id: int = Query(ge=1),
) -> dict:
    return _run(customer_panel.merge_preview, from_id, into_id)


@router.get("/customers/settings")
def customers_settings() -> dict:
    return _run(customer_panel.get_settings)


@router.put("/customers/settings")
def update_customers_settings(payload: CustomerSettingsIn) -> dict:
    return _run(
        customer_panel.update_settings,
        payload.model_dump(exclude_unset=True),
    )


@router.post("/customers/merge")
def customers_merge(payload: CustomerMergeIn) -> dict:
    return _run(
        customer_panel.merge,
        payload.from_id,
        payload.into_id,
        reason=payload.reason,
    )


@router.post("/customers/scan-signals")
def customers_scan_signals(
    limit: int | None = Query(default=None, ge=1, le=5000),
) -> dict:
    return _run(customer_panel.scan_signals, limit=limit)


@router.get("/customers")
def list_customers(
    kind: str | None = Query(default="customer", pattern="^(customer|supplier|all)$"),
    status_filter: str | None = Query(default=None, alias="status"),
    segment: str | None = Query(default=None, pattern="^[a-z_]+$"),
    risk: str | None = Query(default=None, pattern="^(low|medium|high)$"),
    zone_id: int | None = Query(default=None, ge=1),
    tag: str | None = Query(default=None, max_length=40),
    has_sales: bool | None = None,
    q: str | None = Query(default=None, max_length=120),
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "last_seen_at",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return _run(
        customer_panel.list_customers,
        kind=None if kind == "all" else kind,
        status=status_filter,
        segment=segment,
        risk=risk,
        zone_id=zone_id,
        tag=tag,
        has_sales=has_sales,
        q=q,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/customers/{customer_id}")
def get_customer(customer_id: int) -> dict:
    return _run(customer_panel.get_detail, customer_id)


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: int, payload: CustomerPatch) -> dict:
    return _run(
        customer_panel.update_profile,
        customer_id,
        payload.model_dump(exclude_unset=True),
    )


@router.get("/customers/{customer_id}/contact")
def reveal_customer_contact(
    customer_id: int,
    reveal: bool = False,
) -> dict:
    if not reveal:
        detail = _run(customer_panel.get_detail, customer_id)
        return {
            "customer_id": customer_id,
            "revealed": False,
            "phone_masked": detail["customer"].get("phone_masked"),
            "address_masked": detail["customer"].get("address_masked"),
            "contacts": detail.get("contacts", []),
        }
    return _run(customer_panel.reveal_contact, customer_id)


@router.post("/customers/{customer_id}/tags")
def add_customer_tag(customer_id: int, payload: CustomerTagIn) -> dict:
    return _run(
        customer_panel.add_tag,
        customer_id,
        payload.tag,
        reason=payload.reason,
    )


@router.delete("/customers/{customer_id}/tags/{tag}")
def remove_customer_tag(customer_id: int, tag: str) -> dict:
    return _run(customer_panel.remove_tag, customer_id, tag)


@router.post("/customers/{customer_id}/notes")
def add_customer_note(customer_id: int, payload: CustomerNoteIn) -> dict:
    return _run(customer_panel.add_note, customer_id, payload.body)


@router.post("/customers/{customer_id}/signals/{signal_id}/confirm")
def confirm_customer_signal(customer_id: int, signal_id: int) -> dict:
    return _run(customer_panel.confirm_signal, customer_id, signal_id)


@router.post("/customers/{customer_id}/signals/{signal_id}/dismiss")
def dismiss_customer_signal(customer_id: int, signal_id: int) -> dict:
    return _run(customer_panel.dismiss_signal, customer_id, signal_id)


@router.post("/customers/{customer_id}/block")
def block_customer(customer_id: int) -> dict:
    return _run(customer_panel.block, customer_id)


@router.post("/customers/{customer_id}/unblock")
def unblock_customer(customer_id: int) -> dict:
    return _run(customer_panel.unblock, customer_id)
