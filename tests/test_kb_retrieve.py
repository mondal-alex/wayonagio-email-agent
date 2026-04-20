"""Unit tests for kb/retrieve.py runtime path."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wayonagio_email_agent.kb import config as config_module
from wayonagio_email_agent.kb import retrieve as kb_retrieve
from wayonagio_email_agent.kb.chunk import Chunk
from wayonagio_email_agent.kb.store import write_index


def _enable_kb(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KB_RAG_FOLDER_IDS", "rag-root")
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


def test_retrieve_propagates_embedding_failures(monkeypatch, tmp_path):
    """Embedding-API outages must not silently degrade to ungrounded drafts."""
    _seed_artifacts(tmp_path)
    _enable_kb(monkeypatch, tmp_path)

    def fake_embed_query(text, *, model):
        raise RuntimeError("no network")

    monkeypatch.setattr(kb_retrieve.embed, "embed_query", fake_embed_query)

    with pytest.raises(RuntimeError, match="no network"):
        kb_retrieve.retrieve("anything")


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


def test_missing_artifacts_raise_loudly(monkeypatch, tmp_path):
    """No artifact published yet → fail the draft, do not silently ungroound."""
    _enable_kb(monkeypatch, tmp_path)
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "gemini/gemini-embedding-001")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    kb_retrieve.reset_cache()

    with pytest.raises(kb_retrieve.KBUnavailableError, match="kb-ingest"):
        kb_retrieve.retrieve("x")


def test_failed_load_is_not_cached(monkeypatch, tmp_path):
    """Transient artifact-missing must not wedge the process forever."""
    _enable_kb(monkeypatch, tmp_path)
    kb_retrieve.reset_cache()

    with pytest.raises(kb_retrieve.KBUnavailableError):
        kb_retrieve.retrieve("x")

    _seed_artifacts(tmp_path)
    monkeypatch.setattr(
        kb_retrieve.embed,
        "embed_query",
        lambda text, *, model: np.array([1.0, 0.0], dtype=np.float32),
    )
    hits = kb_retrieve.retrieve("x")
    assert len(hits) == 2


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

    with pytest.raises(kb_retrieve.KBUnavailableError):
        kb_retrieve.retrieve("x")


def test_config_snapshot_matches_expectations(monkeypatch, tmp_path):
    _enable_kb(monkeypatch, tmp_path)
    cfg = config_module.load()
    assert cfg.rag_folder_ids == ("rag-root",)
    assert cfg.local_dir == str(tmp_path)
    assert cfg.top_k == 2


def test_index_raises_when_embedding_model_mismatches(monkeypatch, tmp_path):
    """If KB_EMBEDDING_MODEL is rotated without re-ingest, the stored vectors
    have the wrong dimension. Refusing here surfaces the misconfiguration
    before it becomes a hallucinated draft."""
    _seed_artifacts(tmp_path)
    _enable_kb(monkeypatch, tmp_path)
    monkeypatch.setenv("KB_EMBEDDING_MODEL", "other/provider-model")
    kb_retrieve.reset_cache()

    with pytest.raises(kb_retrieve.KBUnavailableError, match="Re-run `kb-ingest`"):
        kb_retrieve.retrieve("anything")
