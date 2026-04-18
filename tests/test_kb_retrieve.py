"""Unit tests for kb/retrieve.py runtime path."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wayonagio_email_agent.kb import config as config_module
from wayonagio_email_agent.kb import retrieve as kb_retrieve
from wayonagio_email_agent.kb.chunk import Chunk
from wayonagio_email_agent.kb.store import write_index


def _enable_kb(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KB_ENABLED", "true")
    monkeypatch.setenv("KB_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("KB_GCS_URI", "")
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "fake/embed")
    monkeypatch.setenv("KB_TOP_K", "2")
    kb_retrieve.reset_cache()


def _seed_artifacts(tmp_path: Path) -> None:
    chunks = [
        Chunk(index=0, text="Machu Picchu tour is 1 day.", source_id="a", source_name="a.md", source_path="root / a.md"),
        Chunk(index=1, text="Sacred Valley tour is 2 days.", source_id="b", source_name="b.md", source_path="root / b.md"),
    ]
    embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    write_index(
        tmp_path / "kb_index.sqlite",
        chunks,
        embeddings,
        embedding_model="fake/embed",
        source_file_count=2,
    )


def test_returns_nothing_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("KB_ENABLED", "false")
    kb_retrieve.reset_cache()
    assert kb_retrieve.retrieve("machu picchu price") == []


def test_retrieves_top_k(monkeypatch, tmp_path):
    _seed_artifacts(tmp_path)
    _enable_kb(monkeypatch, tmp_path)

    def fake_embed_query(text, *, model):
        assert model == "fake/embed"
        return np.array([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(kb_retrieve.embed, "embed_query", fake_embed_query)

    hits = kb_retrieve.retrieve("Tell me about Machu Picchu")
    assert len(hits) == 2
    assert hits[0].text.startswith("Machu Picchu")
    assert hits[0].score >= hits[1].score


def test_retrieve_returns_empty_when_embedding_fails(monkeypatch, tmp_path, caplog):
    _seed_artifacts(tmp_path)
    _enable_kb(monkeypatch, tmp_path)

    def fake_embed_query(text, *, model):
        raise RuntimeError("no network")

    monkeypatch.setattr(kb_retrieve.embed, "embed_query", fake_embed_query)

    caplog.set_level("WARNING")
    assert kb_retrieve.retrieve("anything") == []
    assert any("embedding failed" in rec.message for rec in caplog.records)


def test_format_reference_block_is_safe_for_empty():
    assert kb_retrieve.format_reference_block([]) == ""


def test_format_reference_block_includes_source_paths(monkeypatch, tmp_path):
    _seed_artifacts(tmp_path)
    _enable_kb(monkeypatch, tmp_path)

    def fake_embed_query(text, *, model):
        return np.array([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(kb_retrieve.embed, "embed_query", fake_embed_query)

    hits = kb_retrieve.retrieve("anything")
    block = kb_retrieve.format_reference_block(hits)
    assert "REFERENCE MATERIAL" in block
    assert "root / a.md" in block
    assert "Machu Picchu tour" in block


def test_missing_artifacts_are_non_fatal(monkeypatch, tmp_path):
    # KB enabled but no artifacts ingested yet — must not raise.
    _enable_kb(monkeypatch, tmp_path)
    # retrieve() needs to bail out before embedding because the index is empty.
    # We don't patch embed here — if retrieve() mistakenly tried to embed we'd
    # get a provider error, which would fail the test.
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "gemini/text-embedding-004")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    kb_retrieve.reset_cache()
    assert kb_retrieve.retrieve("x") == []


def test_reset_cache_forces_reload(monkeypatch, tmp_path):
    _seed_artifacts(tmp_path)
    _enable_kb(monkeypatch, tmp_path)

    def fake_embed_query(text, *, model):
        return np.array([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(kb_retrieve.embed, "embed_query", fake_embed_query)

    hits = kb_retrieve.retrieve("x")
    assert len(hits) == 2

    (tmp_path / "kb_index.sqlite").unlink()
    kb_retrieve.reset_cache()

    assert kb_retrieve.retrieve("x") == []


def test_config_snapshot_matches_expectations(monkeypatch, tmp_path):
    _enable_kb(monkeypatch, tmp_path)
    cfg = config_module.load()
    assert cfg.enabled is True
    assert cfg.local_dir == str(tmp_path)
    assert cfg.top_k == 2


def test_index_is_dropped_when_embedding_model_mismatches(monkeypatch, tmp_path, caplog):
    """If KB_EMBEDDING_MODEL is rotated without re-ingest, the stored vectors
    have the wrong dimension. Dropping the index here prevents a runtime
    matmul crash on every draft."""
    _seed_artifacts(tmp_path)
    _enable_kb(monkeypatch, tmp_path)
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "other/provider-model")
    kb_retrieve.reset_cache()

    caplog.set_level("WARNING")
    assert kb_retrieve.retrieve("anything") == []
    assert any(
        "Re-run `kb-ingest`" in rec.message for rec in caplog.records
    ), "Operator must be told why retrieval went silent."
