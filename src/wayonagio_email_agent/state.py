"""SQLite-backed state store for the scanner.

Tracks which message IDs have already been scanned so the scanner does not
repeatedly classify the same unread mail across restarts.

Schema:
    processed_messages(
        message_id TEXT PRIMARY KEY,
        outcome TEXT NOT NULL,
        processed_at TEXT NOT NULL
    )

Outcome values are intentionally simple:
    drafted
    non_travel
    thread_has_draft

DB path is taken from SCANNER_STATE_DB env var (default: scanner_state.db).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _db_path() -> str:
    return os.environ.get("SCANNER_STATE_DB", "scanner_state.db")


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id   TEXT PRIMARY KEY,
            outcome      TEXT NOT NULL DEFAULT 'drafted',
            processed_at TEXT NOT NULL
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(processed_messages)").fetchall()
    }
    if "outcome" not in columns:
        conn.execute(
            "ALTER TABLE processed_messages ADD COLUMN outcome TEXT NOT NULL DEFAULT 'drafted'"
        )
    conn.commit()
    return conn


def is_processed(message_id: str) -> bool:
    """Return True if *message_id* is already in the processed table."""
    return get_outcome(message_id) is not None


def get_outcome(message_id: str) -> str | None:
    """Return the stored scanner outcome for *message_id*, if any."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT outcome FROM processed_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return row[0] if row else None


def mark_processed(message_id: str, outcome: str = "drafted") -> None:
    """Persist a scanner outcome for *message_id* with the current UTC time."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO processed_messages (message_id, outcome, processed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                outcome = excluded.outcome,
                processed_at = excluded.processed_at
            """,
            (message_id, outcome, now),
        )
    logger.debug("Marked message %s as processed with outcome=%s.", message_id, outcome)
