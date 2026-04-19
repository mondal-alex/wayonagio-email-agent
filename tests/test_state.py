"""Unit tests for state.py."""

from __future__ import annotations

import gc
import sqlite3
import warnings

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
        state._schema_verified.discard(str(db_path))

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

    def test_no_resource_warning_under_repeated_calls(self, monkeypatch, tmp_path):
        """Long-running scanner regression: connections must close deterministically.

        sqlite3.Connection.__exit__ commits but does NOT close. Without the
        contextlib.closing wrapper, every is_processed/mark_processed call
        leaks a handle. Hundreds of calls would emit ResourceWarning under a
        warnings-enabled test run; this test fails on that behaviour returning.
        """
        db_path = tmp_path / "scanner_state.db"
        monkeypatch.setenv("SCANNER_STATE_DB", str(db_path))
        state._schema_verified.discard(str(db_path))

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            for i in range(50):
                state.mark_processed(f"msg-{i}", outcome="drafted")
                state.is_processed(f"msg-{i}")
            gc.collect()

        leak_warnings = [
            w for w in recorded if issubclass(w.category, ResourceWarning)
        ]
        assert leak_warnings == [], (
            f"State store leaked sqlite3 connections: {[str(w.message) for w in leak_warnings]}"
        )

    def test_schema_check_is_cached(self, monkeypatch, tmp_path):
        """Second connection to the same DB must skip the PRAGMA / ALTER pass.

        Anchors the warm-call optimisation so a future refactor cannot
        re-introduce a per-call schema check. We verify by wrapping
        ``sqlite3.connect`` to record every ``execute`` call against the
        connection it returns.
        """
        db_path = tmp_path / "scanner_state.db"
        monkeypatch.setenv("SCANNER_STATE_DB", str(db_path))
        state._schema_verified.discard(str(db_path))

        executed_sql: list[str] = []
        original_connect = sqlite3.connect

        class _TrackingConnection:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                executed_sql.append(sql)
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __enter__(self):
                self._real.__enter__()
                return self

            def __exit__(self, *args):
                return self._real.__exit__(*args)

        def tracking_connect(*args, **kwargs):
            return _TrackingConnection(original_connect(*args, **kwargs))

        monkeypatch.setattr(state.sqlite3, "connect", tracking_connect)

        state.mark_processed("msg-1")
        assert str(db_path) in state._schema_verified
        executed_sql.clear()

        state.mark_processed("msg-2")
        assert not any("CREATE TABLE" in sql.upper() for sql in executed_sql), (
            f"Schema CREATE TABLE re-ran on warm call: {executed_sql}"
        )
        assert not any("PRAGMA" in sql.upper() for sql in executed_sql), (
            f"Schema PRAGMA re-ran on warm call: {executed_sql}"
        )
