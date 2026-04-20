"""Embedding generation via LiteLLM.

Mirrors the design of :mod:`wayonagio_email_agent.llm.client`: provider-agnostic
via LiteLLM's ``embedding()`` API, provider-specific knobs (api_base for Ollama,
api_key for Gemini) forwarded only when they apply.

We batch requests because every embedding provider accepts batched input and
it's dramatically cheaper / faster than one request per chunk. The default
batch size is provider-aware, though: Gemini's ``gemini-embedding-001`` is
primarily designed for single-input ``embedContent`` calls (the synchronous
``batchEmbedContents`` endpoint LiteLLM uses is not even listed in the model's
``supportedGenerationMethods``) and the free tier caps at ≈30k TPM, so a
64-chunk batch at ≈500 tokens per chunk is already over budget and reliably
429s. For Gemini we default to much smaller batches with inter-batch pacing;
for Ollama and other self-hosted providers with no rate limit we keep the
large default.
"""

from __future__ import annotations

import logging
import os
import time

import litellm
import numpy as np

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True

_DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"

# Provider-aware batch sizing. Numbers are tuned to stay safely under the
# tightest documented free-tier quota for each provider, so a cold "run it
# and go get coffee" ingest succeeds on the defaults. Paid-tier users with
# higher quotas can raise via ``KB_EMBED_BATCH_SIZE``.
_BATCH_SIZE_BY_PROVIDER = {
    # ≈4 chunks × ≈500 tokens = ≈2k tokens/batch, well under 30k TPM even
    # if other usage is happening. 64 chunks ÷ 4 = 16 batches.
    "gemini": 4,
}
_DEFAULT_BATCH_SIZE = 64

# Inter-batch pacing. Sleeping briefly between successful batches keeps the
# cumulative TPM/RPM from spiking inside a single 60-second window. Gemini
# free tier: 100 RPM, 30k TPM — at batch_size=4 and avg 500 tokens, a 3s
# pace yields ≤20 req/min and ≤10k tokens/min, comfortably under both caps.
# Providers with no rate limit get zero overhead.
_INTER_BATCH_SLEEP_BY_PROVIDER = {
    "gemini": 3.0,
}
_DEFAULT_INTER_BATCH_SLEEP = 0.0

# Retry policy for rate-limit (HTTP 429) responses. A brief quota burst is
# common on free tier during yearly ingest; we back off and retry rather
# than aborting the whole run. Non-rate-limit errors still fail fast
# because retrying them just delays the inevitable.
_DEFAULT_MAX_RATE_LIMIT_RETRIES = 5
_INITIAL_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 60.0


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
    batch_size: int | None = None,
) -> np.ndarray:
    """Embed *texts* with *model* and return an ``(n, d)`` float32 matrix.

    ``batch_size`` defaults to a provider-aware value (small for Gemini so
    the free-tier TPM quota doesn't 429, large for Ollama/other providers
    with no rate limit). Pass an explicit value to override, or set the
    ``KB_EMBED_BATCH_SIZE`` environment variable.

    Raises :class:`RuntimeError` if the provider returns an empty result for
    any text — we refuse to persist a silently broken index.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    provider = _provider(model)
    resolved_batch_size = (
        batch_size
        if batch_size is not None
        else _default_batch_size(provider)
    )
    inter_batch_sleep = _inter_batch_sleep(provider)
    base_kwargs = _provider_kwargs(model)
    vectors: list[list[float]] = []
    max_retries = _max_rate_limit_retries()
    batch_count = (len(texts) + resolved_batch_size - 1) // resolved_batch_size

    if batch_count > 1:
        logger.info(
            "Embedding %d chunk(s) in %d batch(es) of %d (model=%s, "
            "inter-batch pacing %.1fs).",
            len(texts), batch_count, resolved_batch_size, model,
            inter_batch_sleep,
        )

    for index, start in enumerate(
        range(0, len(texts), resolved_batch_size), start=1
    ):
        if index > 1 and inter_batch_sleep > 0:
            time.sleep(inter_batch_sleep)
        batch = texts[start : start + resolved_batch_size]
        response = _embed_batch_with_retry(
            batch,
            model=model,
            base_kwargs=base_kwargs,
            max_retries=max_retries,
            batch_index=index,
            batch_count=batch_count,
        )

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


def _default_batch_size(provider: str) -> int:
    """Resolve the effective batch size, honoring env override."""
    raw = os.environ.get("KB_EMBED_BATCH_SIZE")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "KB_EMBED_BATCH_SIZE=%r is not an integer; falling back to "
                "provider default.", raw,
            )
        else:
            if value > 0:
                return value
            logger.warning(
                "KB_EMBED_BATCH_SIZE=%d is not positive; falling back to "
                "provider default.", value,
            )
    return _BATCH_SIZE_BY_PROVIDER.get(provider, _DEFAULT_BATCH_SIZE)


def _inter_batch_sleep(provider: str) -> float:
    """Seconds to sleep between successful batches, honoring env override."""
    raw = os.environ.get("KB_EMBED_INTER_BATCH_SECONDS")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "KB_EMBED_INTER_BATCH_SECONDS=%r is not a number; falling "
                "back to provider default.", raw,
            )
        else:
            return max(0.0, value)
    return _INTER_BATCH_SLEEP_BY_PROVIDER.get(provider, _DEFAULT_INTER_BATCH_SLEEP)


def _max_rate_limit_retries() -> int:
    """Resolve ``KB_EMBED_MAX_RETRIES`` with a sane default and floor of zero."""
    raw = os.environ.get("KB_EMBED_MAX_RETRIES")
    if not raw:
        return _DEFAULT_MAX_RATE_LIMIT_RETRIES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "KB_EMBED_MAX_RETRIES=%r is not an integer; using default %d.",
            raw, _DEFAULT_MAX_RATE_LIMIT_RETRIES,
        )
        return _DEFAULT_MAX_RATE_LIMIT_RETRIES
    return max(0, value)


def _embed_batch_with_retry(
    batch: list[str],
    *,
    model: str,
    base_kwargs: dict,
    max_retries: int,
    batch_index: int,
    batch_count: int,
):
    """Call ``litellm.embedding`` with exponential backoff on rate-limit errors.

    Returns the raw LiteLLM response on success; re-raises the underlying
    exception on non-rate-limit errors or after *max_retries* rate-limit
    retries are exhausted. Sleep is resolved via ``time.sleep`` through the
    module-level ``time`` binding so tests can monkey-patch it.
    """
    attempt = 0
    while True:
        try:
            return litellm.embedding(model=model, input=batch, **base_kwargs)
        except litellm.RateLimitError as exc:
            if attempt >= max_retries:
                logger.error(
                    "Embedding call rate-limited beyond max retries "
                    "(model=%s, retries=%d). Last error: %s",
                    model, max_retries, exc,
                )
                raise
            delay = min(
                _INITIAL_BACKOFF_SECONDS * (2 ** attempt),
                _MAX_BACKOFF_SECONDS,
            )
            logger.warning(
                "Rate limited on embedding batch %d/%d (model=%s, "
                "attempt %d/%d); sleeping %.0fs before retry.",
                batch_index, batch_count, model,
                attempt + 1, max_retries, delay,
            )
            time.sleep(delay)
            attempt += 1
        except Exception as exc:
            logger.error("Embedding call failed (model=%s): %s", model, exc)
            raise


def embed_query(text: str, *, model: str) -> np.ndarray:
    """Embed a single query string. Returns a 1-D ``(d,)`` vector."""
    matrix = embed_texts([text], model=model)
    if matrix.size == 0:
        raise RuntimeError("embed_query got an empty result.")
    return matrix[0]
