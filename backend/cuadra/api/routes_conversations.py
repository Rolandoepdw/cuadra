"""Conversations panel endpoints (phase F1).

Read-only views over the chat plus the administrative close action. Sending
messages from the dashboard is intentionally absent: the anti-ban policy keeps
all outbound traffic inside the paced queue (plan §7).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from cuadra import db
from cuadra.api.auth import require_pin
from cuadra.domain import conversation_panel
from cuadra.domain.errors import NotFound

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])


def _run(fn, *args, **kwargs):
    conn = db.connect()
    try:
        return fn(conn, *args, **kwargs)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    finally:
        conn.close()


# `/summary` must be declared before `/{conversation_id}`: otherwise FastAPI
# tries to parse "summary" as an int and answers 422.
@router.get("/conversations/summary")
def conversations_summary() -> dict:
    return _run(conversation_panel.summary)


@router.get("/conversations")
def list_conversations(
    kind: str | None = Query(default=None, pattern="^(customer|supplier)$"),
    status_filter: str | None = Query(default=None, alias="status"),
    identity: str | None = None,
    channel: str | None = None,
    stage: str | None = None,
    needs_attention: bool | None = None,
    has_escalation: bool | None = None,
    has_sale: bool | None = None,
    q: str | None = Query(default=None, max_length=120),
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "updated_at",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return _run(
        conversation_panel.list_conversations,
        kind=kind,
        status=status_filter,
        identity=identity,
        channel=channel,
        stage=stage,
        needs_attention=needs_attention,
        has_escalation=has_escalation,
        has_sale=has_sale,
        q=q,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    message_limit: int = Query(default=100, ge=1, le=300),
) -> dict:
    return _run(
        conversation_panel.get_detail,
        conversation_id,
        message_limit=message_limit,
    )


@router.get("/conversations/{conversation_id}/messages")
def list_conversation_messages(
    conversation_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(default=None, ge=1),
) -> dict:
    return _run(
        conversation_panel.messages_page,
        conversation_id,
        limit=limit,
        before_id=before_id,
    )


@router.post("/conversations/{conversation_id}/close")
def close_conversation(conversation_id: int) -> dict:
    return _run(conversation_panel.close, conversation_id)
