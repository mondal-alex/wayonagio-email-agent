"""Embedding generation via LiteLLM.

Mirrors the design of :mod:`wayonagio_email_agent.llm.client`: provider-agnostic
via LiteLLM's ``embedding()`` API, provider-specific knobs (api_base for Ollama,
api_key for Gemini) forwarded only when they apply.

We batch requests (default 64 texts per call) because every embedding provider
accepts batched input and it's dramatically cheaper / faster than one request
per chunk.
"""

from __future__ import annotations

import logging
import os

import litellm
import numpy as np

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True

_DEFAULT_BATCH_SIZE = 64
_DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"


def _provider(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def _provider_kwargs(model: str) -> dict:
    provider = _provider(model)
    kwargs: dict = {}
    if provider == "ollama":
        kwargs["api_base"] = os.environ.get(
            "OLLAMA_BASE_URL", _DEFAULT_OLLAMA_BASE_URL
        )
    elif provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "KB_EMBEDDING_MODEL is a Gemini model but GEMINI_API_KEY is not set. "
                "Set GEMINI_API_KEY in your environment / .env / Secret Manager."
            )
        kwargs["api_key"] = api_key
    return kwargs


def embed_texts(
    texts: list[str],
    *,
    model: str,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> np.ndarray:
    """Embed *texts* with *model* and return an ``(n, d)`` float32 matrix.

    Raises :class:`RuntimeError` if the provider returns an empty result for
    any text — we refuse to persist a silently broken index.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    base_kwargs = _provider_kwargs(model)
    vectors: list[list[float]] = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        try:
            response = litellm.embedding(model=model, input=batch, **base_kwargs)
        except Exception as exc:
            logger.error("Embedding call failed (model=%s): %s", model, exc)
            raise

        # LiteLLM exposes `data` as an attribute on EmbeddingResponse objects,
        # but some providers wrap the response in a plain dict. Handle both.
        if hasattr(response, "data") and response.data is not None:
            data = response.data
        elif hasattr(response, "get"):
            data = response.get("data", [])
        else:
            data = []
        if len(data) != len(batch):
            raise RuntimeError(
                f"Embedding provider returned {len(data)} vectors for "
                f"{len(batch)} inputs (model={model})."
            )
        for entry in data:
            embedding = entry["embedding"] if isinstance(entry, dict) else entry.embedding
            if not embedding:
                raise RuntimeError(
                    f"Embedding provider returned empty vector (model={model})."
                )
            vectors.append(list(embedding))

    matrix = np.asarray(vectors, dtype=np.float32)
    logger.debug(
        "Embedded %d texts with %s (dim=%d).", len(texts), model, matrix.shape[1]
    )
    return matrix


def embed_query(text: str, *, model: str) -> np.ndarray:
    """Embed a single query string. Returns a 1-D ``(d,)`` vector."""
    matrix = embed_texts([text], model=model)
    if matrix.size == 0:
        raise RuntimeError("embed_query got an empty result.")
    return matrix[0]
