"""Finance endpoints: summary, sales, commission ledger and operator actions."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from cuadra import config, db
from cuadra.ai import queue
from cuadra.api.auth import require_pin
from cuadra.domain import sales

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])


class CancelSaleIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class FollowupMuteIn(BaseModel):
    muted: bool = True


def _run(fn, *args, **kwargs):
    conn = db.connect()
    try:
        return fn(conn, *args, **kwargs)
    except sales.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except sales.Invalid as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    finally:
        conn.close()


@router.get("/finance/summary")
def finance_summary() -> dict:
    return _run(sales.summary)


@router.get("/finance/sales")
def list_sales(
    status_filter: str | None = Query(default=None, alias="status"),
    currency: str | None = None,
    supplier_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items = _run(
        sales.list_sales,
        status=status_filter,
        currency=currency,
        supplier_id=supplier_id,
        limit=limit,
        offset=offset,
    )
    total = _run(
        sales.count_sales,
        status=status_filter,
        currency=currency,
        supplier_id=supplier_id,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/finance/sales/{sale_id}")
def get_sale(sale_id: int) -> dict:
    result = _run(sales.get_sale, sale_id)
    result["ledger"] = _run(sales.get_ledger, sale_id)
    return result


@router.get("/finance/followups")
def followup_sales(
    hours: float = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Stale open sales, for the dashboard reminder only."""
    items = _run(sales.followups, hours=hours, limit=limit)
    return {"hours": hours, "items": items}


@router.get("/finance/ledger")
def list_ledger(
    status_filter: str | None = Query(default=None, alias="status"),
    currency: str | None = None,
    supplier_id: int | None = None,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items = _run(
        sales.list_ledger,
        status=status_filter,
        currency=currency,
        supplier_id=supplier_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    total = _run(
        sales.count_ledger,
        status=status_filter,
        currency=currency,
        supplier_id=supplier_id,
        date_from=date_from,
        date_to=date_to,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _enqueue_dispatch(sale_id: int) -> None:
    queue.enqueue(
        "dispatch_sale", {"sale_id": sale_id}, priority=config.PRIORITY_TCP
    )


@router.post("/finance/approve/{sale_id}")
def approve_sale(sale_id: int) -> dict:
    sale = _run(sales.approve, sale_id)
    _enqueue_dispatch(sale_id)
    return sale


@router.post("/finance/dispatch/{sale_id}")
def dispatch_sale(sale_id: int) -> dict:
    def _prepare(conn):
        sale = sales.get_sale(conn, sale_id)
        if sale["status"] == "NEEDS_OPERATOR":
            sale = sales.arm_dispatch(conn, sale_id)
        if sale["status"] != "PENDING_DISPATCH":
            raise sales.Invalid(
                f"cannot dispatch a sale in status {sale['status']}"
            )
        return sale

    sale = _run(_prepare)
    _enqueue_dispatch(sale_id)
    return sale


@router.post("/finance/settle/{sale_id}")
def settle_sale(sale_id: int) -> dict:
    return _run(sales.settle, sale_id)


@router.post("/finance/cancel/{sale_id}")
def cancel_sale(sale_id: int, payload: CancelSaleIn | None = None) -> dict:
    reason = payload.reason if payload is not None else ""

    def _cancel(conn):
        before = sales.get_sale(conn, sale_id)
        return before, sales.cancel(conn, sale_id, reason)

    before, sale = _run(_cancel)
    # Only the TCP that already received the order needs the cancellation.
    if before["status"] in sales.TCP_AWARE_STATUSES:
        queue.enqueue(
            "notify_tcp_cancel", {"sale_id": sale_id}, priority=config.PRIORITY_TCP
        )
    if sale.get("conversation_id"):
        queue.enqueue(
            "notify_sale_cancelled",
            {"sale_id": sale_id},
            priority=config.PRIORITY_CUSTOMER,
        )
    return sale


@router.post("/finance/followup/{sale_id}")
def send_followup(sale_id: int) -> dict:
    """Send the customer reminder now, even if the automatic mode is off.

    Manual is still one-shot: a sale that already got its reminder is rejected
    instead of silently doing nothing. The job itself re-reads the sale, so it
    also no-ops if the status changed between this call and the worker.
    """

    def _prepare(conn):
        sale = sales.get_sale(conn, sale_id)
        if sale["status"] not in sales.FOLLOWUP_MESSAGE_STATUSES:
            raise sales.Invalid(
                f"cannot remind a sale in status {sale['status']}"
            )
        if sale.get("followup_sent_at"):
            raise sales.Invalid("this sale already got its reminder")
        if not sale["conversation_id"]:
            raise sales.Invalid("this sale has no conversation to reply to")
        return sale

    sale = _run(_prepare)
    queue.enqueue(
        "sale_followup",
        {"sale_id": sale_id, "trigger": "manual"},
        priority=config.PRIORITY_CUSTOMER,
    )
    return sale


@router.post("/finance/followup/{sale_id}/mute")
def mute_followup(sale_id: int, payload: FollowupMuteIn | None = None) -> dict:
    """Operator opt-out per sale; `muted: false` re-arms it."""
    muted = True if payload is None else payload.muted
    return _run(sales.mute_followup, sale_id, muted)
