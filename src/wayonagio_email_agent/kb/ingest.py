"""End-to-end KB ingest pipeline.

Run via the ``kb-ingest`` CLI subcommand (or as a Cloud Run Job on a
Scheduler trigger):

1. Resolve :class:`config.KBConfig` from the environment.
2. Walk every RAG folder, extract text, chunk it, embed the chunks, and write
   them to ``kb_index.sqlite``.
3. Publish the index artifact to GCS (or the configured local dir).

The pipeline is deliberately single-pass and stateless — the only persistent
output is the index. Re-running is idempotent; we always rebuild from scratch
because (a) Drive content changes are rarely large enough for incremental to
pay off, and (b) "rebuild from scratch" is the easiest semantics to reason
about when a reply looks wrong.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from wayonagio_email_agent.kb import artifact, drive, embed, extract, store
from wayonagio_email_agent.kb.chunk import Chunk, chunk_text
from wayonagio_email_agent.kb.config import KBConfig, load as load_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    rag_source_count: int
    rag_chunk_count: int
    embedding_dim: int
    index_destination: str


def run(*, config: KBConfig | None = None, service: Any | None = None) -> IngestResult:
    """Run the full ingest pipeline and publish the index artifact.

    *service* is only overridden in tests — production passes ``None`` and
    the Drive wrapper builds a real service from the existing OAuth token.
    """
    cfg = config or load_config()
    if not cfg.enabled:
        raise RuntimeError(
            "KB ingest was invoked but KB_ENABLED is not true. Refusing to publish "
            "artifacts that will be ignored at runtime."
        )
    if not cfg.rag_folder_ids:
        raise RuntimeError(
            "KB ingest needs KB_RAG_FOLDER_IDS to be set."
        )

    with tempfile.TemporaryDirectory(prefix="kb_ingest_") as tmp:
        tmp_path = Path(tmp)

        rag_sources, chunks, embeddings = _ingest_rag(cfg, service=service)

        # Safety guard: when the operator pointed us at RAG folders but we
        # found zero usable content (permissions issue, everything failed to
        # extract, folder was emptied), refuse to publish an empty index. An
        # empty index would silently replace a previously-working one at
        # runtime and degrade retrieval to nothing without anyone noticing.
        if rag_sources == 0:
            raise RuntimeError(
                "KB ingest resolved zero usable RAG sources from "
                f"{len(cfg.rag_folder_ids)} configured folder(s). Refusing to "
                "publish an empty index that would overwrite the previous one. "
                "Check the ingest logs for per-file skip warnings, verify "
                "folder IDs, and confirm the service account has drive.readonly."
            )

        index_local = tmp_path / cfg.index_filename
        store.write_index(
            index_local,
            chunks,
            embeddings,
            embedding_model=cfg.embedding_model,
            source_file_count=rag_sources,
        )

        index_destination = artifact.upload_artifact(
            cfg, index_local, cfg.index_filename
        )

    result = IngestResult(
        rag_source_count=rag_sources,
        rag_chunk_count=len(chunks),
        embedding_dim=int(embeddings.shape[1]) if embeddings.size else 0,
        index_destination=index_destination,
    )
    logger.info(
        "KB ingest complete: rag_sources=%d, chunks=%d, dim=%d.",
        result.rag_source_count,
        result.rag_chunk_count,
        result.embedding_dim,
    )
    return result


def _ingest_rag(config: KBConfig, *, service: Any | None):
    """Return ``(source_file_count, chunks, embeddings)`` for the RAG bucket."""
    all_chunks: list[Chunk] = []
    sources_touched = 0

    for folder_id in config.rag_folder_ids:
        files = drive.list_folder(
            folder_id,
            recursive=config.rag_recursive,
            include_mime_types=config.include_mime_types,
            service=service,
        )

        for drive_file in files:
            try:
                payload = drive.read_file(drive_file, service=service)
                text = extract.extract_text(drive_file, payload)
            except extract.ExtractionError as exc:
                logger.warning("Skipping %s: %s", drive_file.path, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "RAG source %s failed to read (%s); skipping.",
                    drive_file.path,
                    exc,
                )
                continue

            if not text.strip():
                continue

            sources_touched += 1
            all_chunks.extend(
                chunk_text(
                    text,
                    source_id=drive_file.id,
                    source_name=drive_file.name,
                    source_path=drive_file.path,
                )
            )

    if not all_chunks:
        return sources_touched, [], np.zeros((0, 0), dtype=np.float32)

    texts = [chunk.text for chunk in all_chunks]
    embeddings = embed.embed_texts(texts, model=config.embedding_model)
    return sources_touched, all_chunks, embeddings
