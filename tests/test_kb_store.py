"""Unit tests for kb/store.py."""

from __future__ import annotations

import gc
import warnings

import numpy as np

from wayonagio_email_agent.kb.chunk import Chunk
from wayonagio_email_agent.kb.store import load_index, write_index


def _chunk(i: int, text: str) -> Chunk:
    return Chunk(
        index=i,
        text=text,
        source_id=f"sid-{i}",
        source_name=f"doc-{i}.md",
        source_path=f"root / doc-{i}.md",
    )


def test_write_and_load_round_trip(tmp_path):
    chunks = [_chunk(0, "hello"), _chunk(1, "world")]
    embeddings = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)

    index_path = tmp_path / "kb_index.sqlite"
    write_index(
        index_path,
        chunks,
        embeddings,
        embedding_model="gemini/text-embedding-004",
        source_file_count=2,
    )

    assert index_path.exists()

    loaded = load_index(index_path)
    assert bool(loaded) is True
    assert loaded.meta.embedding_model == "gemini/text-embedding-004"
    assert loaded.meta.dimension == 3
    assert loaded.meta.source_file_count == 2
    assert loaded.texts == ["hello", "world"]
    assert loaded.source_paths == ["root / doc-0.md", "root / doc-1.md"]
    assert loaded.embeddings.shape == (2, 3)

    norms = np.linalg.norm(loaded.embeddings, axis=1)
    np.testing.assert_allclose(norms, np.ones(2), rtol=1e-6)


def test_top_k_returns_highest_cosine_similarity(tmp_path):
    chunks = [_chunk(0, "hello"), _chunk(1, "world"), _chunk(2, "cusco")]
    embeddings = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.9, 0.1, 0.0],
        ],
        dtype=np.float32,
    )
    index_path = tmp_path / "kb_index.sqlite"
    write_index(
        index_path,
        chunks,
        embeddings,
        embedding_model="test/model",
        source_file_count=3,
    )

    loaded = load_index(index_path)
    hits = loaded.top_k(np.array([1.0, 0.0, 0.0]), k=2)

    assert len(hits) == 2
    assert hits[0].text == "hello"
    assert hits[0].score >= hits[1].score
    assert hits[1].text == "cusco"


def test_empty_index_is_safe(tmp_path):
    index_path = tmp_path / "kb_index.sqlite"
    write_index(
        index_path,
        chunks=[],
        embeddings=np.zeros((0, 0), dtype=np.float32),
        embedding_model="test/model",
        source_file_count=0,
    )

    loaded = load_index(index_path)
    assert bool(loaded) is False
    assert loaded.top_k(np.array([1.0, 0.0, 0.0]), k=3) == []


def test_top_k_handles_zero_query_vector(tmp_path):
    chunks = [_chunk(0, "hi")]
    embeddings = np.array([[1.0, 0.0]], dtype=np.float32)
    index_path = tmp_path / "kb_index.sqlite"
    write_index(
        index_path,
        chunks,
        embeddings,
        embedding_model="test/model",
        source_file_count=1,
    )
    loaded = load_index(index_path)
    assert loaded.top_k(np.array([0.0, 0.0]), k=1) == []


def test_write_and_load_do_not_leak_sqlite_connections(tmp_path):
    """Regression: ``sqlite3.Connection.__exit__`` commits but does NOT close.
    Without ``contextlib.closing`` around ``sqlite3.connect``, every
    ``write_index`` / ``load_index`` call leaks a file descriptor and
    eventually surfaces as ``ResourceWarning: unclosed database`` (which
    blows up the test suite under ``pytest -W error``).
    """
    chunks = [_chunk(0, "hello")]
    embeddings = np.array([[1.0, 0.0]], dtype=np.float32)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        for i in range(20):
            index_path = tmp_path / f"kb_index_{i}.sqlite"
            write_index(
                index_path,
                chunks,
                embeddings,
                embedding_model="test/model",
                source_file_count=1,
            )
            load_index(index_path)
        gc.collect()

    leaks = [w for w in recorded if issubclass(w.category, ResourceWarning)]
    assert leaks == [], (
        f"kb/store leaked sqlite connections: {[str(w.message) for w in leaks]}"
    )


def test_write_replaces_existing_file(tmp_path):
    index_path = tmp_path / "kb_index.sqlite"
    write_index(
        index_path,
        chunks=[_chunk(0, "first")],
        embeddings=np.array([[1.0, 0.0]], dtype=np.float32),
        embedding_model="test/model",
        source_file_count=1,
    )
    write_index(
        index_path,
        chunks=[_chunk(0, "second")],
        embeddings=np.array([[0.0, 1.0]], dtype=np.float32),
        embedding_model="test/model",
        source_file_count=1,
    )

    loaded = load_index(index_path)
    assert loaded.texts == ["second"]
