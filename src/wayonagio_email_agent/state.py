"""SQLite-backed state store for the scanner.

Tracks which message IDs have already been processed (draft created) so the
scanner never creates duplicate drafts across restarts.

Schema:
    processed_messages(message_id TEXT PRIMARY KEY, processed_at TEXT)

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
            processed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def is_processed(message_id: str) -> bool:
    """Return True if *message_id* is already in the processed table."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return row is not None


def mark_processed(message_id: str) -> None:
    """Insert *message_id* into the processed table with the current UTC time."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
            (message_id, now),
        )
    logger.debug("Marked message %s as processed.", message_id)
