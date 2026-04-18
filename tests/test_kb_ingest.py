"""End-to-end ingest tests with mocked Drive and embeddings."""

from __future__ import annotations

import numpy as np
import pytest

from wayonagio_email_agent.kb import ingest
from wayonagio_email_agent.kb import drive as drive_module
from wayonagio_email_agent.kb import extract as extract_module
from wayonagio_email_agent.kb.config import KBConfig
from wayonagio_email_agent.kb.store import load_index


def _cfg(tmp_path, *, rag_ids=("rag-root",)) -> KBConfig:
    return KBConfig(
        enabled=True,
        rag_folder_ids=rag_ids,
        rag_recursive=True,
        include_mime_types=("text/markdown", "application/pdf"),
        embedding_model="fake/embed",
        gcs_uri="",
        local_dir=str(tmp_path / "artifacts"),
        top_k=4,
    )


def _patch_drive(monkeypatch, files_by_folder, payloads):
    def fake_list(folder_id, *, recursive, include_mime_types, service=None, **_kw):
        return files_by_folder.get(folder_id, [])

    def fake_read(drive_file, *, service=None):
        return payloads[drive_file.id]

    monkeypatch.setattr(ingest.drive, "list_folder", fake_list)
    monkeypatch.setattr(ingest.drive, "read_file", fake_read)


def _patch_embed(monkeypatch):
    def fake_embed_texts(texts, *, model, **_kw):
        # Deterministic pseudo-embeddings: one axis per text, unit norm.
        vectors = np.eye(len(texts), dtype=np.float32)
        return vectors

    monkeypatch.setattr(ingest.embed, "embed_texts", fake_embed_texts)


def test_ingest_writes_index(monkeypatch, tmp_path):
    rag_files = [
        drive_module.DriveFile(
            id="r1",
            name="MachuPicchu.md",
            mime_type="text/markdown",
            path="Tours / MachuPicchu.md",
            modified_time="t",
        ),
        drive_module.DriveFile(
            id="r2",
            name="SacredValley.md",
            mime_type="text/markdown",
            path="Tours / SacredValley.md",
            modified_time="t",
        ),
    ]
    _patch_drive(
        monkeypatch,
        files_by_folder={"rag-root": rag_files},
        payloads={
            "r1": b"Machu Picchu tour info. " * 20,
            "r2": b"Sacred Valley details. " * 20,
        },
    )
    _patch_embed(monkeypatch)

    cfg = _cfg(tmp_path, rag_ids=("rag-root",))
    result = ingest.run(config=cfg, service=object())

    assert result.rag_source_count == 2
    assert result.rag_chunk_count >= 2
    assert result.embedding_dim == result.rag_chunk_count

    index_path = tmp_path / "artifacts" / "kb_index.sqlite"
    assert index_path.exists()

    loaded = load_index(index_path)
    assert loaded.meta.source_file_count == 2
    assert loaded.meta.embedding_model == "fake/embed"
    assert "Machu Picchu" in " ".join(loaded.texts) + " " + " ".join(loaded.source_paths)


def test_ingest_refuses_when_disabled(tmp_path):
    cfg = KBConfig(
        enabled=False,
        rag_folder_ids=("r",),
        rag_recursive=True,
        include_mime_types=("text/markdown",),
        embedding_model="x/y",
        gcs_uri="",
        local_dir=str(tmp_path),
        top_k=4,
    )
    with pytest.raises(RuntimeError, match="KB_ENABLED"):
        ingest.run(config=cfg)


def test_ingest_refuses_when_no_rag_folders_configured(tmp_path):
    cfg = KBConfig(
        enabled=True,
        rag_folder_ids=(),
        rag_recursive=True,
        include_mime_types=("text/markdown",),
        embedding_model="x/y",
        gcs_uri="",
        local_dir=str(tmp_path),
        top_k=4,
    )
    with pytest.raises(RuntimeError, match="KB_RAG_FOLDER_IDS"):
        ingest.run(config=cfg)


def test_ingest_refuses_to_publish_empty_index_when_rag_configured(
    monkeypatch, tmp_path, caplog
):
    """Regression guard: if the operator pointed us at RAG folders but we
    found zero usable sources, we MUST NOT overwrite a previously-good index
    with an empty one. Abort loudly instead."""
    _patch_drive(
        monkeypatch,
        files_by_folder={"rag-root": []},
        payloads={},
    )
    _patch_embed(monkeypatch)

    cfg = _cfg(tmp_path, rag_ids=("rag-root",))
    caplog.set_level("WARNING")

    with pytest.raises(RuntimeError, match="zero usable RAG sources"):
        ingest.run(config=cfg, service=object())

    # And critically: no artifacts should have landed on disk.
    assert not (tmp_path / "artifacts" / "kb_index.sqlite").exists()


def test_ingest_skips_unextractable_files(monkeypatch, tmp_path, caplog):
    rag_files = [
        drive_module.DriveFile(
            id="ok",
            name="good.md",
            mime_type="text/markdown",
            path="root / good.md",
            modified_time="t",
        ),
        drive_module.DriveFile(
            id="bad",
            name="broken.md",
            mime_type="text/markdown",
            path="root / broken.md",
            modified_time="t",
        ),
    ]
    _patch_drive(
        monkeypatch,
        files_by_folder={"rag-root": rag_files},
        payloads={"ok": b"Hello", "bad": b""},
    )

    original = extract_module.extract_text

    def flaky_extract(df, payload):
        if df.id == "bad":
            raise extract_module.ExtractionError("no text")
        return original(df, payload)

    monkeypatch.setattr(ingest.extract, "extract_text", flaky_extract)
    _patch_embed(monkeypatch)

    caplog.set_level("WARNING")
    result = ingest.run(config=_cfg(tmp_path), service=object())

    assert result.rag_source_count == 1
    assert any("broken.md" in rec.message for rec in caplog.records)
