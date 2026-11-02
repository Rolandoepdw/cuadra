"""System audit trail (`events` table).

Every row carries an operational `category` so the dashboard Logs view can
group events into independent windows without re-mapping event types in SQL.
"""

import json

SOURCES = ("system", "bot", "dashboard", "scheduler", "llm", "stt")

CATEGORIES = ("publications", "conversations", "entities", "system")

# Campaign sending/scheduling. Checked first because `broadcast_posted` is
# written with source "bot" but belongs to the publications window.
PUBLICATION_EVENTS = frozenset(
    {
        "broadcast_scheduled",
        "broadcast_posted",
        "broadcast_post_failed",
        "broadcast_no_targets",
        "broadcast_cycle_complete",
        "broadcast_send_now",
        "broadcasts_reevaluated",
        "broadcast_publishing_paused",
        "broadcast_publishing_resumed",
        "broadcast_enabled",
        "broadcast_disabled",
    }
)

# Bot/LLM/STT activity. The source fallback keeps future conversation events
# in the right window without touching this module. Escalation retries/timeouts
# are written by the scheduler but belong to the conversation flow.
CONVERSATION_SOURCES = frozenset({"bot", "llm", "stt"})
CONVERSATION_EVENTS = frozenset(
    {
        "reply_generated",
        "message_sent",
        "escalation_created",
        "escalation_sent",
        "escalation_resolved",
        "escalation_retry",
        "escalation_timeout",
        "tcp_message_received",
        "tcp_reply_unmatched",
        "nlu_invalid_output",
        "summary_updated",
        "audio_transcribed",
    }
)

# CRUD and configuration changes. Suffixes cover new `*_created`, `*_updated`,
# `*_deleted`, `*_deactivated`, `*_applied` and `*_saved` events automatically.
ENTITY_SUFFIXES = (
    "_created",
    "_updated",
    "_deleted",
    "_deactivated",
    "_applied",
    "_saved",
)


def category_for(source: str, event_type: str, entity: str | None = None) -> str:
    if event_type in PUBLICATION_EVENTS:
        return "publications"
    if source in CONVERSATION_SOURCES or event_type in CONVERSATION_EVENTS:
        return "conversations"
    if event_type.endswith(ENTITY_SUFFIXES):
        return "entities"
    if entity in {
        "categories",
        "zones",
        "suppliers",
        "products",
        "product_images",
        "broadcast_groups",
        "broadcasts",
        "settings",
    }:
        return "entities"
    return "system"


def log(
    conn,
    source: str,
    event_type: str,
    *,
    category: str | None = None,
    entity=None,
    entity_id=None,
    details=None,
) -> None:
    if source not in SOURCES:
        source = "system"
    if category not in CATEGORIES:
        category = category_for(source, event_type, entity)
    conn.execute(
        """INSERT INTO events (source, type, category, entity, entity_id, details_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            source,
            event_type,
            category,
            entity,
            entity_id,
            json.dumps(details, ensure_ascii=False) if details is not None else None,
        ),
    )
    conn.commit()
