"""SQLite access: connection, WAL, foreign_keys and migration runner."""

import sqlite3
from datetime import datetime, timezone

from cuadra import config


def connect(timeout: float = 30) -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply migrations/*.sql in order. Returns those applied in this run."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               name TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )
    applied = {
        r["name"]
        for r in conn.execute("SELECT name FROM schema_migrations").fetchall()
    }
    done: list[str] = []
    for path in sorted(config.MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (path.name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        done.append(path.name)
    return done


def integrity_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA quick_check").fetchone()
    return row is not None and row[0] == "ok"
