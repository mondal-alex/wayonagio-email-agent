"""Unit tests for kb/doctor.py.

The doctor module is the operator-facing source-of-truth for "is the KB
healthy?", so these tests err on the side of anchoring visible behavior
(exact report fields, healthy/unhealthy classification, graceful
degradation when the artifact is missing or corrupt) rather than
exercising implementation details.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wayonagio_email_agent.kb import config as kb_config
from wayonagio_email_agent.kb import doctor, store
from wayonagio_email_agent.kb.chunk import Chunk


def _chunks_with_sources(pairs: list[tuple[str, int]]) -> list[Chunk]:
    """Build chunks grouped by source path for a given list of
    (source_path, n_chunks) pairs.
    """
    out: list[Chunk] = []
    for source_path, n in pairs:
        for idx in range(n):
            out.append(
                Chunk(
                    source_id=f"sid-{source_path}-{idx}",
                    source_name=source_path.split("/")[-1].strip(),
                    source_path=source_path,
                    index=idx,
                    text=f"chunk {idx} for {source_path}",
                )
            )
    return out


def _write_fake_index(
    path: Path,
    *,
    sources: list[tuple[str, int]],
    embedding_model: str,
    dim: int = 4,
) -> int:
    """Write a fresh sqlite index at *path* using the real store layer.

    Using ``store.write_index`` directly instead of hand-crafting SQL
    anchors the tests to the real production schema — if the store
    format changes, we want this test's assertions about chunk counts
    and per-source breakdown to follow automatically.
    """
    chunks = _chunks_with_sources(sources)
    embeddings = np.random.default_rng(0).standard_normal(
        (len(chunks), dim)
    ).astype(np.float32)
    store.write_index(
        path,
        chunks,
        embeddings,
        embedding_model=embedding_model,
        source_file_count=len({s for s, _ in sources}),
    )
    return len(chunks)


@pytest.fixture
def kb_env(monkeypatch, tmp_path):
    """Point the doctor module at a writable, isolated ``KB_LOCAL_DIR``.

    Every test in this file wants the same baseline: KB_RAG_FOLDER_IDS
    satisfied (otherwise ``config.load`` raises before we get anywhere),
    a clean local artifact dir under tmp_path, GCS disabled.
    """
    local_dir = tmp_path / "kb"
    local_dir.mkdir()
    monkeypatch.setenv("KB_RAG_FOLDER_IDS", "drive-folder-id")
    monkeypatch.setenv("KB_LOCAL_DIR", str(local_dir))
    monkeypatch.delenv("KB_GCS_URI", raising=False)
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "gemini/gemini-embedding-001")
    # Exemplars deliberately disabled by default so doctor tests focus on
    # the KB path; dedicated tests below re-enable them.
    monkeypatch.delenv("KB_EXEMPLAR_FOLDER_IDS", raising=False)

    # The exemplar loader holds a process-level cache. If an earlier test
    # in the session populated it, doctor's exemplar section would read
    # stale state. Reset once per test so each one starts from a
    # known-empty cache.
    from wayonagio_email_agent.exemplars import loader as exemplar_loader

    exemplar_loader.reset()
    yield local_dir
    exemplar_loader.reset()


class TestBuildReport:
    def test_reports_healthy_when_index_present_and_model_matches(self, kb_env):
        _write_fake_index(
            kb_env / "kb_index.sqlite",
            sources=[
                ("Root / Tours 2026 / Machu Picchu.pdf", 18),
                ("Root / Tours 2026 / Sacred Valley.pdf", 6),
                ("Root / FAQs.md", 3),
            ],
            embedding_model="gemini/gemini-embedding-001",
        )

        report = doctor.build_report()

        assert report.healthy is True
        assert report.artifact_available is True
        assert report.index_loaded is True
        assert report.chunk_count == 27
        assert report.embedding_model_matches is True
        assert report.index_meta is not None
        assert report.index_meta.embedding_model == "gemini/gemini-embedding-001"
        assert report.issues == []

        # Per-source breakdown is sorted desc by chunk count — operators
        # scan top-down for "the big one" and this ordering matches that
        # mental model.
        paths = [s.source_path for s in report.sources]
        counts = [s.chunk_count for s in report.sources]
        assert paths == [
            "Root / Tours 2026 / Machu Picchu.pdf",
            "Root / Tours 2026 / Sacred Valley.pdf",
            "Root / FAQs.md",
        ]
        assert counts == [18, 6, 3]

    def test_reports_missing_artifact_with_actionable_issue(self, kb_env):
        # No index file written — download_artifact returns None and we
        # must not crash, must flag artifact_available=False, and must
        # emit a one-line "run kb-ingest" remediation.
        report = doctor.build_report()

        assert report.healthy is False
        assert report.artifact_available is False
        assert report.index_loaded is False
        assert report.chunk_count == 0
        assert report.sources == []
        assert any("kb-ingest" in issue for issue in report.issues)

    def test_reports_model_mismatch(self, kb_env, monkeypatch):
        """Classic upgrade footgun: ingest was run with the old
        embedding model, runtime was switched to a new one, dimensions
        no longer match. We must flag this before retrieval silently
        returns garbage at runtime.
        """
        _write_fake_index(
            kb_env / "kb_index.sqlite",
            sources=[("Root / FAQs.md", 3)],
            embedding_model="gemini/text-embedding-004",  # retired, stale
        )
        monkeypatch.setenv("KB_EMBEDDING_MODEL", "gemini/gemini-embedding-001")

        report = doctor.build_report()

        assert report.healthy is False, (
            "an index built with a stale embedding model must register "
            "as unhealthy — retrieval would otherwise silently mix old "
            "vectors into a new query space (e.g. 768-dim text-embedding-004 "
            "chunks ranked against 3072-dim gemini-embedding-001 queries)."
        )
        assert report.embedding_model_matches is False
        assert any(
            "text-embedding-004" in issue and "gemini-embedding-001" in issue
            for issue in report.issues
        )

    def test_corrupt_index_is_reported_gracefully(self, kb_env):
        """A truncated/corrupt SQLite file must be surfaced as an issue
        — not a traceback — so the operator sees "run kb-ingest" instead
        of stderr spam.
        """
        bad_path = kb_env / "kb_index.sqlite"
        bad_path.write_bytes(b"this is not a sqlite database")

        report = doctor.build_report()

        assert report.artifact_available is True
        assert report.index_loaded is False
        assert report.healthy is False
        assert any("corrupt" in issue.lower() or "could not be loaded" in issue for issue in report.issues)

    def test_missing_kb_folder_ids_raises(self, monkeypatch, tmp_path):
        """``KBConfigError`` is load-bearing: it's the one failure mode
        that MUST be re-raised by ``build_report`` (rather than captured
        as an issue) because an unconfigured agent can't meaningfully be
        diagnosed further.
        """
        monkeypatch.delenv("KB_RAG_FOLDER_IDS", raising=False)
        monkeypatch.setenv("KB_LOCAL_DIR", str(tmp_path))

        with pytest.raises(kb_config.KBConfigError):
            doctor.build_report()

    def test_gcs_destination_is_rendered_with_index_filename(
        self, kb_env, monkeypatch
    ):
        """Operators copy-paste the artifact destination into
        ``gsutil ls`` / ``gcloud storage`` — it must include the full
        object path, not just the bucket URI they configured.
        """
        monkeypatch.setenv("KB_GCS_URI", "gs://my-bucket/ops")

        # We don't want the GCS client to actually be called; the config
        # branch chooses the GCS path because KB_GCS_URI is set, and
        # ``artifact.download_artifact`` will try to import google.cloud.
        # Intercept by monkeypatching the module function.
        from wayonagio_email_agent.kb import artifact as artifact_module

        monkeypatch.setattr(
            artifact_module, "download_artifact", lambda *a, **kw: None
        )

        report = doctor.build_report()

        assert report.artifact_destination == "gs://my-bucket/ops/kb_index.sqlite"

    def test_exemplar_pool_is_included_when_present(
        self, kb_env, monkeypatch
    ):
        from wayonagio_email_agent.exemplars import loader as exemplar_loader
        from wayonagio_email_agent.exemplars.source import Exemplar

        _write_fake_index(
            kb_env / "kb_index.sqlite",
            sources=[("Root / FAQs.md", 2)],
            embedding_model="gemini/gemini-embedding-001",
        )
        # Populate the exemplar cache directly — we're testing that
        # doctor reads it, not that the loader works.
        exemplar_loader._cache = [
            Exemplar(title="Cancellation policy", text="...", source_id="a"),
            Exemplar(title="Arrival instructions", text="...", source_id="b"),
        ]

        report = doctor.build_report()

        assert report.exemplar_count == 2
        assert "Cancellation policy" in report.exemplar_titles
        assert "Arrival instructions" in report.exemplar_titles


class TestFormatReport:
    def test_healthy_report_renders_expected_sections(self, kb_env):
        _write_fake_index(
            kb_env / "kb_index.sqlite",
            sources=[("Root / FAQs.md", 2)],
            embedding_model="gemini/gemini-embedding-001",
        )
        report = doctor.build_report()
        text = doctor.format_report(report)

        assert "KB status: HEALTHY" in text
        assert "Config:" in text
        assert "Index:" in text
        assert "Sources indexed (1):" in text
        assert "Root / FAQs.md" in text
        assert "Exemplars:" in text
        # No issues => no "Issues:" section.
        assert "Issues:" not in text

    def test_unhealthy_report_lists_issues(self, kb_env):
        report = doctor.build_report()  # no index written
        text = doctor.format_report(report)

        assert "KB status: UNHEALTHY" in text
        assert "Issues:" in text
        assert "kb-ingest" in text

    def test_format_report_truncates_sources(self, kb_env):
        """An index with hundreds of sources must not flood the operator
        console — the top-N breakdown is what they care about.
        """
        sources = [(f"Root / doc-{i:03d}.md", 1) for i in range(30)]
        _write_fake_index(
            kb_env / "kb_index.sqlite",
            sources=sources,
            embedding_model="gemini/gemini-embedding-001",
        )
        report = doctor.build_report()
        text = doctor.format_report(report, max_sources=5)

        # 5 shown, remainder collapsed into one line.
        assert "25 more source(s) not shown" in text


class TestIngestAge:
    def test_returns_empty_for_blank_timestamp(self):
        assert doctor._ingest_age("") == ""

    def test_returns_empty_for_unparseable_timestamp(self):
        assert doctor._ingest_age("not-a-date") == ""

    def test_formats_recent_ingest_as_sub_hour(self, monkeypatch):
        """Freshly-published indexes are the common case after a
        scheduled ingest run. The age string must not show '0h ago'.
        """
        from datetime import datetime, timedelta, timezone

        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert doctor._ingest_age(ts) == "<1h ago"

    def test_formats_multi_day_age(self):
        from datetime import datetime, timedelta, timezone

        ts = (datetime.now(timezone.utc) - timedelta(days=2, hours=3)).isoformat()
        age = doctor._ingest_age(ts)
        assert age.startswith("2d ")
        assert "h ago" in age
