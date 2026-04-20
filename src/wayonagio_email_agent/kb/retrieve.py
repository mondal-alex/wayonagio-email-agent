"""Runtime-side KB access: vector retrieval.

The API / scanner processes only read the KB. They never talk to Google Drive
directly — that's the ingest Job's job. On first use we:

1. Resolve :func:`config.load`.
2. Download the index artifact to ``/tmp`` (or a configurable cache dir) and
   load it into memory.

Failures are **fatal**, not silent. The KB is now a hard dependency of every
draft: an agent that quietly falls back to ungrounded text is worse than no
agent at all because staff can't tell which drafts they should trust. When
the KB is unavailable, :class:`KBUnavailableError` is raised and the caller
must refuse to draft.

The draft-only invariant is still sacred — we never call ``messages.send``,
only ``drafts.create``. Refusing to draft is preferable to drafting an
ungrounded reply that staff might send unmodified.
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


class KBUnavailableError(RuntimeError):
    """Raised when retrieval cannot be served from a usable KB index.

    Covers: no artifact published yet, artifact download failed, on-disk index
    is corrupt, index is empty, or the index was built with a different
    embedding model than the runtime is configured to use.
    """


# ---------------------------------------------------------------------------
# Process-level cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state: "_KBState | None" = None


class _KBState:
    __slots__ = ("index", "cache_dir")

    def __init__(self, index: LoadedIndex, cache_dir: Path):
        self.index = index
        self.cache_dir = cache_dir


def _default_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "wayonagio_kb_cache"


def _load_state(config: config_module.KBConfig) -> _KBState:
    cache_dir = _default_cache_dir()

    index_path = artifact.download_artifact(config, config.index_filename, cache_dir)
    if index_path is None:
        raise KBUnavailableError(
            "KB index artifact could not be downloaded. Run `kb-ingest` to "
            "publish kb_index.sqlite to the configured destination "
            "(KB_GCS_URI or KB_LOCAL_DIR)."
        )

    try:
        index = load_index(index_path)
    except Exception as exc:
        raise KBUnavailableError(
            f"Could not load KB index at {index_path}: {exc}"
        ) from exc

    if not index:
        raise KBUnavailableError(
            f"KB index at {index_path} is empty. Re-run `kb-ingest` to rebuild it."
        )

    if (
        index.meta.embedding_model
        and index.meta.embedding_model != config.embedding_model
    ):
        # A mismatched embedding model means the query vector has a different
        # dimension than the stored vectors. The fix is to re-run `kb-ingest`
        # with the current model.
        raise KBUnavailableError(
            f"KB index was built with embedding model "
            f"{index.meta.embedding_model!r} but KB_EMBEDDING_MODEL is "
            f"currently {config.embedding_model!r}. Re-run `kb-ingest` to "
            "rebuild the index with the new model."
        )

    logger.info(
        "KB index loaded: %d chunks, model=%s, ingested=%s.",
        index.embeddings.shape[0],
        index.meta.embedding_model,
        index.meta.ingested_at,
    )
    return _KBState(index=index, cache_dir=cache_dir)


def _ensure_loaded(cfg: config_module.KBConfig) -> _KBState:
    """Return the process-wide KB state, loading it on first use.

    A failed load is **not** cached — transient outages (GCS hiccup, race with
    an in-progress ingest) self-heal on the next request rather than wedging
    the process until restart.

    The caller passes in ``cfg`` so ``retrieve`` can reuse the same config
    snapshot for both the cache-miss load and the downstream query (top_k,
    embedding model). Loading the config twice per request was harmless but
    created a narrow window where concurrent env-var changes could produce
    an inconsistent picture.
    """
    global _state

    if _state is None:
        with _lock:
            if _state is None:
                _state = _load_state(cfg)
    return _state


def reset_cache() -> None:
    """Drop the in-memory KB state so the next access reloads from disk/GCS.

    Exposed for tests and for a future admin ``POST /kb/reload`` endpoint.
    """
    global _state
    with _lock:
        _state = None


# ---------------------------------------------------------------------------
# Public API consumed by llm/client.py
# ---------------------------------------------------------------------------

def retrieve(query: str, *, top_k: int | None = None) -> list[ScoredChunk]:
    """Return up to *top_k* chunks most similar to *query*.

    Raises :class:`KBUnavailableError` if the KB index cannot be loaded, or
    propagates the underlying provider exception if query embedding fails.
    The caller (``llm/client.generate_reply``) refuses to draft on either.
    """
    cfg = config_module.load()
    state = _ensure_loaded(cfg)

    effective_k = top_k if top_k is not None else cfg.top_k
    if effective_k <= 0:
        return []

    query_vector = embed.embed_query(query, model=cfg.embedding_model)
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
