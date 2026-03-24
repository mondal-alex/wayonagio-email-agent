"""Unit tests for state.py."""

from __future__ import annotations

import sqlite3

from wayonagio_email_agent import state


class TestStateStore:
    def test_mark_processed_persists_outcome(self, monkeypatch, tmp_path):
        db_path = tmp_path / "scanner_state.db"
        monkeypatch.setenv("SCANNER_STATE_DB", str(db_path))

        state.mark_processed("msg-1", outcome="non_travel")

        assert state.is_processed("msg-1") is True
        assert state.get_outcome("msg-1") == "non_travel"

    def test_mark_processed_updates_existing_outcome(self, monkeypatch, tmp_path):
        db_path = tmp_path / "scanner_state.db"
        monkeypatch.setenv("SCANNER_STATE_DB", str(db_path))

        state.mark_processed("msg-1", outcome="non_travel")
        state.mark_processed("msg-1", outcome="drafted")

        assert state.get_outcome("msg-1") == "drafted"

    def test_migrates_existing_database_without_outcome_column(self, monkeypatch, tmp_path):
        db_path = tmp_path / "scanner_state.db"
        monkeypatch.setenv("SCANNER_STATE_DB", str(db_path))

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE processed_messages (
                message_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
            ("msg-legacy", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        assert state.get_outcome("msg-legacy") == "drafted"
