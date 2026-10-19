"""System audit trail (`events` table)."""

import json

SOURCES = ("system", "bot", "dashboard", "scheduler", "llm", "stt")


def log(conn, source: str, event_type: str, *, entity=None, entity_id=None, details=None) -> None:
    if source not in SOURCES:
        source = "system"
    conn.execute(
        """INSERT INTO events (source, type, entity, entity_id, details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (
            source,
            event_type,
            entity,
            entity_id,
            json.dumps(details, ensure_ascii=False) if details is not None else None,
        ),
    )
    conn.commit()
