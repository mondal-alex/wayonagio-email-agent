"""Runtime-side KB access: vector retrieval.

The API / scanner processes only read the KB. They never talk to Google Drive
directly — that's the ingest Job's job. On first use we:

1. Resolve :func:`config.load`.
2. If KB is disabled, return empty results forever.
3. Otherwise download the index artifact to ``/tmp`` (or a configurable cache
   dir) and load it into memory.

Any failure along the way is **non-fatal**: we log a warning and the agent
keeps drafting without KB augmentation. The draft-only invariant is sacred;
the KB is a quality lever on top of it.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from wayonagio_email_agent.kb import artifact, config as config_module
from wayonagio_email_agent.kb import embed
from wayonagio_email_agent.kb.store import LoadedIndex, ScoredChunk, load_index

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-level cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state: "_KBState | None" = None


class _KBState:
    __slots__ = ("index", "cache_dir")

    def __init__(self, index: LoadedIndex | None, cache_dir: Path):
        self.index = index
        self.cache_dir = cache_dir


def _default_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "wayonagio_kb_cache"


def _load_state(config: config_module.KBConfig) -> _KBState:
    cache_dir = _default_cache_dir()
    index: LoadedIndex | None = None

    index_path = artifact.download_artifact(config, config.index_filename, cache_dir)
    if index_path is not None:
        try:
            index = load_index(index_path)
            logger.info(
                "KB index loaded: %d chunks, model=%s, ingested=%s.",
                index.embeddings.shape[0],
                index.meta.embedding_model,
                index.meta.ingested_at,
            )
            if (
                index.meta.embedding_model
                and index.meta.embedding_model != config.embedding_model
            ):
                # Warn loudly: a mismatched embedding model means the query
                # vector has a different dimension than the stored vectors and
                # retrieval will silently return zero hits (or throw in top_k).
                # The fix is to re-run `kb-ingest` with the current model.
                logger.warning(
                    "KB index was built with model %r but KB_EMBEDDING_MODEL is "
                    "currently %r. Re-run `kb-ingest` to rebuild the index with "
                    "the new model; retrieval will be disabled until then.",
                    index.meta.embedding_model,
                    config.embedding_model,
                )
                index = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load KB index at %s: %s", index_path, exc)

    return _KBState(index=index, cache_dir=cache_dir)


def _ensure_loaded() -> _KBState | None:
    """Return the process-wide KB state, loading it on first use."""
    global _state
    cfg = config_module.load()
    if not cfg.enabled:
        return None

    if _state is None:
        with _lock:
            if _state is None:
                _state = _load_state(cfg)
    return _state


def reset_cache() -> None:
    """Drop the in-memory KB state so the next access reloads from disk/GCS.

    Exposed mainly for tests and for a future admin ``POST /kb/reload``
    endpoint. Not called by anything on the hot path.
    """
    global _state
    with _lock:
        _state = None


# ---------------------------------------------------------------------------
# Public API consumed by llm/client.py
# ---------------------------------------------------------------------------

def retrieve(query: str, *, top_k: int | None = None) -> list[ScoredChunk]:
    """Return up to *top_k* chunks most similar to *query*.

    Any failure (embedding error, empty index, KB disabled) returns an empty
    list — the caller is expected to continue drafting without augmentation.
    """
    state = _ensure_loaded()
    if state is None or state.index is None or not state.index:
        return []

    cfg = config_module.load()
    effective_k = top_k if top_k is not None else cfg.top_k
    if effective_k <= 0:
        return []

    try:
        query_vector = embed.embed_query(query, model=cfg.embedding_model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("KB retrieval aborted — query embedding failed: %s", exc)
        return []

    hits = state.index.top_k(query_vector, effective_k)
    if logger.isEnabledFor(logging.INFO) and hits:
        logger.info(
            "KB retrieval: %s",
            ", ".join(f"{hit.source_path} ({hit.score:.3f})" for hit in hits),
        )
    return hits


def format_reference_block(hits: list[ScoredChunk]) -> str:
    """Format retrieved chunks for inclusion in the LLM user prompt.

    Each chunk is labeled with its Drive path so the model can (if asked) cite
    the source, and the block is delimited so the model never mistakes it for
    the client's own words.
    """
    if not hits:
        return ""

    parts = ["--- REFERENCE MATERIAL ---"]
    for hit in hits:
        parts.append(f"Source: {hit.source_path}\n{hit.text}")
    parts.append("--- END REFERENCE MATERIAL ---")
    return "\n\n".join(parts)
