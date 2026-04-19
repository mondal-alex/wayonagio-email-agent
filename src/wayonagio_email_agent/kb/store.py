"""SQLite-backed vector store.

One file, two tables:

* ``chunks``    — text + Drive metadata + embedding BLOB (float32, little-endian).
* ``meta``      — key/value pairs: embedding model, dimension, ingest timestamp,
                  source file count. Written once per ingest; read at load.

Retrieval is brute-force cosine similarity with ``numpy``: at the travel
agency's corpus size (a few thousand chunks max) this is faster than a hosted
vector DB network hop and vastly simpler. See the plan for the upgrade path.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from wayonagio_email_agent.kb.chunk import Chunk

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT NOT NULL,
    source_name    TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    chunk_index    INTEGER NOT NULL,
    text           TEXT NOT NULL,
    embedding      BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ScoredChunk:
    """A chunk returned from retrieval, with its cosine similarity score."""

    text: str
    source_id: str
    source_name: str
    source_path: str
    chunk_index: int
    score: float


@dataclass(frozen=True)
class IndexMeta:
    embedding_model: str
    dimension: int
    ingested_at: str
    source_file_count: int


# ---------------------------------------------------------------------------
# Write path (ingest)
# ---------------------------------------------------------------------------

def write_index(
    path: str | Path,
    chunks: list[Chunk],
    embeddings: np.ndarray,
    *,
    embedding_model: str,
    source_file_count: int,
) -> None:
    """Create a fresh index file at *path*.

    The existing file (if any) is replaced — we never partially update a live
    index because a half-written index would silently degrade retrieval
    quality. Atomic-replace semantics are the caller's responsibility when
    deploying new artifacts.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"chunk count {len(chunks)} != embedding count {len(embeddings)}."
        )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    # ``contextlib.closing`` is mandatory: ``sqlite3.Connection.__exit__``
    # commits the transaction but does NOT close the connection. Without it
    # we leak a file descriptor every ingest, which Python eventually
    # surfaces as ``ResourceWarning: unclosed database`` and which would
    # exhaust ulimit on a long-running scanner over time.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(_SCHEMA)

        conn.executemany(
            """
            INSERT INTO chunks
                (source_id, source_name, source_path, chunk_index, text, embedding)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    chunk.source_id,
                    chunk.source_name,
                    chunk.source_path,
                    chunk.index,
                    chunk.text,
                    np.asarray(vector, dtype=np.float32).tobytes(),
                )
                for chunk, vector in zip(chunks, embeddings, strict=True)
            ),
        )

        dimension = int(embeddings.shape[1]) if embeddings.size else 0
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [
                ("embedding_model", embedding_model),
                ("dimension", str(dimension)),
                ("ingested_at", now),
                ("source_file_count", str(source_file_count)),
                ("chunk_count", str(len(chunks))),
            ],
        )
        conn.commit()

    logger.info(
        "Wrote KB index at %s: %d chunks, dim=%d, model=%s.",
        path,
        len(chunks),
        dimension,
        embedding_model,
    )


# ---------------------------------------------------------------------------
# Read path (runtime)
# ---------------------------------------------------------------------------

@dataclass
class LoadedIndex:
    """Fully-loaded in-memory index ready for cosine similarity search."""

    meta: IndexMeta
    texts: list[str]
    source_ids: list[str]
    source_names: list[str]
    source_paths: list[str]
    chunk_indexes: list[int]
    embeddings: np.ndarray  # shape (n, d), L2-normalized float32.

    def __bool__(self) -> bool:
        return self.embeddings.size > 0

    def top_k(self, query_vector: np.ndarray, k: int) -> list[ScoredChunk]:
        if not self:
            return []
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        norm = np.linalg.norm(query)
        if norm == 0:
            return []
        query_unit = query / norm

        scores = self.embeddings @ query_unit
        if k >= len(scores):
            top_idx = np.argsort(-scores)
        else:
            top_idx = np.argpartition(-scores, k)[:k]
            top_idx = top_idx[np.argsort(-scores[top_idx])]

        return [
            ScoredChunk(
                text=self.texts[i],
                source_id=self.source_ids[i],
                source_name=self.source_names[i],
                source_path=self.source_paths[i],
                chunk_index=self.chunk_indexes[i],
                score=float(scores[i]),
            )
            for i in top_idx
        ]


def load_index(path: str | Path) -> LoadedIndex:
    """Load an on-disk index into memory, L2-normalizing embeddings once."""
    path = Path(path)
    # See note in write_index: __exit__ doesn't close the sqlite3 handle.
    with closing(sqlite3.connect(path)) as conn:
        meta_rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        rows = conn.execute(
            """
            SELECT source_id, source_name, source_path, chunk_index, text, embedding
            FROM chunks
            ORDER BY id
            """
        ).fetchall()

    dimension = int(meta_rows.get("dimension", "0"))
    meta = IndexMeta(
        embedding_model=meta_rows.get("embedding_model", ""),
        dimension=dimension,
        ingested_at=meta_rows.get("ingested_at", ""),
        source_file_count=int(meta_rows.get("source_file_count", "0")),
    )

    if not rows or dimension == 0:
        return LoadedIndex(
            meta=meta,
            texts=[],
            source_ids=[],
            source_names=[],
            source_paths=[],
            chunk_indexes=[],
            embeddings=np.zeros((0, 0), dtype=np.float32),
        )

    texts: list[str] = []
    source_ids: list[str] = []
    source_names: list[str] = []
    source_paths: list[str] = []
    chunk_indexes: list[int] = []
    matrix = np.empty((len(rows), dimension), dtype=np.float32)
    for i, row in enumerate(rows):
        sid, sname, spath, cindex, text, blob = row
        source_ids.append(sid)
        source_names.append(sname)
        source_paths.append(spath)
        chunk_indexes.append(int(cindex))
        texts.append(text)
        matrix[i] = np.frombuffer(blob, dtype=np.float32)

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    return LoadedIndex(
        meta=meta,
        texts=texts,
        source_ids=source_ids,
        source_names=source_names,
        source_paths=source_paths,
        chunk_indexes=chunk_indexes,
        embeddings=matrix,
    )
