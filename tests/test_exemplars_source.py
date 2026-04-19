"""Unit tests for exemplars/source.py.

We stub ``kb.drive`` and ``kb.extract`` so no real Google Drive credentials
are needed. Each test exercises one of the contracts in
``exemplars/source.py``:

* per-Doc failures don't abort the batch
* empty-after-extract Docs are skipped with a WARNING
* parallel fan-out actually fans out (concurrency assertion)
* disabled config short-circuits without touching Drive
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from wayonagio_email_agent.exemplars import source
from wayonagio_email_agent.exemplars.config import ExemplarConfig
from wayonagio_email_agent.kb.drive import DriveFile


def _df(file_id: str, name: str = "Exemplar.gdoc") -> DriveFile:
    return DriveFile(
        id=file_id,
        name=name,
        mime_type="application/vnd.google-apps.document",
        path=f"Exemplars / {name}",
        modified_time="t",
    )


def _enabled_cfg() -> ExemplarConfig:
    return ExemplarConfig(
        folder_ids=("folder-1",),
        include_mime_types=("application/vnd.google-apps.document",),
    )


class TestCollectDisabledShortCircuit:
    def test_disabled_config_returns_empty_without_touching_drive(
        self, monkeypatch
    ):
        called = []
        monkeypatch.setattr(
            source.kb_drive, "list_folder", lambda *a, **kw: called.append(1) or []
        )
        monkeypatch.setattr(
            source.kb_drive,
            "build_drive_service",
            lambda: pytest.fail("Drive service should not be built when disabled"),
        )

        cfg = ExemplarConfig(folder_ids=(), include_mime_types=())
        assert source.collect(cfg, service=object()) == []
        assert called == []


class TestCollectHappyPath:
    def test_returns_sanitized_exemplars_in_title_order(self, monkeypatch):
        files = [
            _df("id-b", "B Exemplar.gdoc"),
            _df("id-a", "A Exemplar.gdoc"),
        ]
        monkeypatch.setattr(
            source.kb_drive, "list_folder", lambda *a, **kw: files
        )
        monkeypatch.setattr(
            source.kb_drive,
            "read_file",
            lambda df, service=None: f"Body of {df.name}".encode(),
        )
        monkeypatch.setattr(
            source.kb_extract,
            "extract_text",
            lambda df, payload: f"Body of {df.name}",
        )

        result = source.collect(_enabled_cfg(), service=object())

        assert [ex.title for ex in result] == ["A Exemplar.gdoc", "B Exemplar.gdoc"]
        assert result[0].text == "Body of A Exemplar.gdoc"
        assert result[0].source_id == "id-a"


class TestCollectFailureIsolation:
    def test_per_doc_failure_is_logged_and_batch_continues(
        self, monkeypatch, caplog
    ):
        files = [_df("ok-1", "Good.gdoc"), _df("bad", "Broken.gdoc")]
        monkeypatch.setattr(
            source.kb_drive, "list_folder", lambda *a, **kw: files
        )

        def _read(df, service=None):
            if df.id == "bad":
                raise RuntimeError("Drive 500")
            return b"good"

        monkeypatch.setattr(source.kb_drive, "read_file", _read)
        monkeypatch.setattr(
            source.kb_extract, "extract_text", lambda df, payload: "good body"
        )

        with caplog.at_level(logging.WARNING, logger=source.__name__):
            result = source.collect(_enabled_cfg(), service=object())

        assert [ex.source_id for ex in result] == ["ok-1"]
        messages = [r.getMessage() for r in caplog.records]
        assert any("Failed to load exemplar" in m and "bad" in m for m in messages)

    def test_empty_after_extract_is_skipped_with_warning(
        self, monkeypatch, caplog
    ):
        files = [_df("blank", "Blank.gdoc")]
        monkeypatch.setattr(
            source.kb_drive, "list_folder", lambda *a, **kw: files
        )
        monkeypatch.setattr(
            source.kb_drive, "read_file", lambda df, service=None: b""
        )
        monkeypatch.setattr(
            source.kb_extract, "extract_text", lambda df, payload: "   \n  "
        )

        with caplog.at_level(logging.WARNING, logger=source.__name__):
            result = source.collect(_enabled_cfg(), service=object())

        assert result == []
        assert any(
            "Skipping empty exemplar Doc" in r.getMessage() for r in caplog.records
        )

    def test_no_drive_files_logs_warning_and_returns_empty(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(source.kb_drive, "list_folder", lambda *a, **kw: [])
        with caplog.at_level(logging.WARNING, logger=source.__name__):
            assert source.collect(_enabled_cfg(), service=object()) == []
        assert any(
            "No exemplar Docs found" in r.getMessage() for r in caplog.records
        )


class TestCollectSanitizesContent:
    def test_pii_in_doc_body_is_redacted_in_returned_exemplar(self, monkeypatch):
        files = [_df("pii", "Leaky.gdoc")]
        monkeypatch.setattr(
            source.kb_drive, "list_folder", lambda *a, **kw: files
        )
        monkeypatch.setattr(
            source.kb_drive, "read_file", lambda df, service=None: b""
        )
        monkeypatch.setattr(
            source.kb_extract,
            "extract_text",
            lambda df, payload: "Email guest@example.com please.",
        )

        result = source.collect(_enabled_cfg(), service=object())

        assert len(result) == 1
        assert "guest@example.com" not in result[0].text
        assert "<EMAIL>" in result[0].text


class TestCollectParallelism:
    """Anchors that ``collect`` actually fans out across the worker pool.

    Without parallelism, cold-start latency for 30 exemplars at ~200ms each
    is ~6s. The plan's performance budget assumes ~1s. If a future refactor
    accidentally serializes the reads (e.g. by introducing a shared lock
    across the per-Doc work), this test fires immediately.
    """

    def test_concurrent_reads_overlap_in_time(self, monkeypatch):
        files = [_df(f"id-{i}", f"Doc-{i:02d}.gdoc") for i in range(8)]
        monkeypatch.setattr(
            source.kb_drive, "list_folder", lambda *a, **kw: files
        )

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        def _read(df, service=None):
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                if in_flight > max_in_flight:
                    max_in_flight = in_flight
            # Sleep long enough that serial execution would never overlap.
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            return b"body"

        monkeypatch.setattr(source.kb_drive, "read_file", _read)
        monkeypatch.setattr(
            source.kb_extract, "extract_text", lambda df, payload: "body"
        )

        result = source.collect(
            _enabled_cfg(), service=object(), max_workers=8
        )

        assert len(result) == 8
        # If the pool is genuinely fanning out, we expect well over 1
        # concurrent read. The exact ceiling depends on scheduler timing;
        # >=2 is the strictly-non-serial threshold.
        assert max_in_flight >= 2, (
            f"Expected concurrent reads, but only saw {max_in_flight} in flight "
            "at once — the worker pool is serializing."
        )
