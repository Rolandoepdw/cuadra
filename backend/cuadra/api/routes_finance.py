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
