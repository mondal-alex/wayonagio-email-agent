"""Unit tests for kb/embed.py."""

from __future__ import annotations

import litellm
import numpy as np
import pytest

from wayonagio_email_agent.kb import embed


class _FakeResponse(dict):
    pass


class TestEmbedTexts:
    def test_returns_empty_matrix_for_empty_input(self):
        result = embed.embed_texts([], model="ollama/any-model")
        assert result.shape == (0, 0)

    def test_batches_texts_and_stacks_vectors(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        calls = []

        def fake_embedding(**kwargs):
            calls.append(kwargs)
            batch = kwargs["input"]
            return _FakeResponse(
                data=[{"embedding": [float(i), 0.0, 0.0]} for i, _ in enumerate(batch)]
            )

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)

        result = embed.embed_texts(
            ["a", "b", "c"], model="ollama/nomic-embed-text", batch_size=2
        )
        assert result.shape == (3, 3)
        assert len(calls) == 2
        assert calls[0]["input"] == ["a", "b"]
        assert calls[1]["input"] == ["c"]
        assert calls[0]["api_base"] == "http://fake:11434"

    def test_requires_gemini_api_key_for_gemini_models(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            embed.embed_texts(["x"], model="gemini/gemini-embedding-001")

    def test_passes_gemini_api_key(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        captured = {}

        def fake_embedding(**kwargs):
            captured.update(kwargs)
            return _FakeResponse(data=[{"embedding": [0.1, 0.2]}])

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)

        embed.embed_texts(["x"], model="gemini/gemini-embedding-001")
        assert captured["api_key"] == "sk-test"

    def test_raises_when_provider_returns_fewer_vectors_than_inputs(self, monkeypatch):
        def fake_embedding(**kwargs):
            return _FakeResponse(data=[{"embedding": [1.0]}])

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        with pytest.raises(RuntimeError, match="returned 1 vectors for 2"):
            embed.embed_texts(["a", "b"], model="ollama/any")

    def test_raises_on_empty_vector(self, monkeypatch):
        def fake_embedding(**kwargs):
            return _FakeResponse(data=[{"embedding": []}])

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        with pytest.raises(RuntimeError, match="empty vector"):
            embed.embed_texts(["a"], model="ollama/any")


class TestProviderAwareDefaults:
    """Gemini's free-tier TPM is tight enough that defaulting to batch_size=64
    (what every other provider wants) reliably 429s on a real corpus. These
    tests pin the contract: Gemini gets small batches + inter-batch pacing by
    default, other providers get the large batch and zero overhead.
    """

    def test_gemini_uses_small_batches_and_pacing_by_default(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        monkeypatch.delenv("KB_EMBED_BATCH_SIZE", raising=False)
        monkeypatch.delenv("KB_EMBED_INTER_BATCH_SECONDS", raising=False)

        call_sizes: list[int] = []
        sleeps: list[float] = []

        def fake_embedding(**kwargs):
            batch = kwargs["input"]
            call_sizes.append(len(batch))
            return _FakeResponse(
                data=[{"embedding": [float(i)]} for i, _ in enumerate(batch)]
            )

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        monkeypatch.setattr(embed.time, "sleep", lambda s: sleeps.append(s))

        # 10 chunks / 4 per batch = 3 batches (4, 4, 2); 2 inter-batch sleeps.
        embed.embed_texts(
            [f"text-{i}" for i in range(10)],
            model="gemini/gemini-embedding-001",
        )
        assert call_sizes == [4, 4, 2]
        assert sleeps == [3.0, 3.0]

    def test_ollama_keeps_large_batches_and_no_pacing(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        monkeypatch.delenv("KB_EMBED_BATCH_SIZE", raising=False)
        monkeypatch.delenv("KB_EMBED_INTER_BATCH_SECONDS", raising=False)

        call_sizes: list[int] = []
        sleeps: list[float] = []

        def fake_embedding(**kwargs):
            batch = kwargs["input"]
            call_sizes.append(len(batch))
            return _FakeResponse(
                data=[{"embedding": [float(i)]} for i, _ in enumerate(batch)]
            )

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        monkeypatch.setattr(embed.time, "sleep", lambda s: sleeps.append(s))

        embed.embed_texts(
            [f"text-{i}" for i in range(70)], model="ollama/nomic-embed-text"
        )
        # 70 / 64 = 2 batches (64, 6); no pacing between them.
        assert call_sizes == [64, 6]
        assert sleeps == []

    def test_env_var_overrides_provider_default(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        monkeypatch.setenv("KB_EMBED_BATCH_SIZE", "16")
        monkeypatch.setenv("KB_EMBED_INTER_BATCH_SECONDS", "0")

        call_sizes: list[int] = []

        def fake_embedding(**kwargs):
            batch = kwargs["input"]
            call_sizes.append(len(batch))
            return _FakeResponse(
                data=[{"embedding": [float(i)]} for i, _ in enumerate(batch)]
            )

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)

        embed.embed_texts(
            [f"t-{i}" for i in range(20)], model="gemini/gemini-embedding-001"
        )
        # 20 / 16 = 2 batches (16, 4).
        assert call_sizes == [16, 4]

    def test_explicit_batch_size_arg_wins_over_env_and_provider(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        monkeypatch.setenv("KB_EMBED_BATCH_SIZE", "8")

        call_sizes: list[int] = []

        def fake_embedding(**kwargs):
            batch = kwargs["input"]
            call_sizes.append(len(batch))
            return _FakeResponse(
                data=[{"embedding": [float(i)]} for i, _ in enumerate(batch)]
            )

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        monkeypatch.setattr(embed.time, "sleep", lambda s: None)

        embed.embed_texts(
            [f"t-{i}" for i in range(5)],
            model="gemini/gemini-embedding-001",
            batch_size=2,
        )
        # Explicit arg beats both the env var (8) and the provider default (4).
        assert call_sizes == [2, 2, 1]

    def test_bad_env_values_fall_back_to_provider_default(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        monkeypatch.setenv("KB_EMBED_BATCH_SIZE", "not-a-number")
        monkeypatch.setenv("KB_EMBED_INTER_BATCH_SECONDS", "also-nonsense")

        call_sizes: list[int] = []

        def fake_embedding(**kwargs):
            batch = kwargs["input"]
            call_sizes.append(len(batch))
            return _FakeResponse(
                data=[{"embedding": [float(i)]} for i, _ in enumerate(batch)]
            )

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        monkeypatch.setattr(embed.time, "sleep", lambda s: None)

        embed.embed_texts(
            [f"t-{i}" for i in range(6)], model="gemini/gemini-embedding-001"
        )
        # Falls back to Gemini default (4), not to the broken env value.
        assert call_sizes == [4, 2]


class TestRateLimitRetry:
    """Rate-limit (429) recovery is a load-bearing property of ingest:
    Gemini free-tier quotas are tight enough that a real yearly corpus
    *will* trip them for a minute at a time, and we must not abort the
    whole run when that happens.
    """

    def _rate_limit_error(self):
        return litellm.RateLimitError(
            message="429 Quota exceeded",
            model="gemini/gemini-embedding-001",
            llm_provider="gemini",
        )

    def test_recovers_from_transient_rate_limit(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        sleeps: list[float] = []
        calls = {"n": 0}

        def fake_embedding(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise self._rate_limit_error()
            batch = kwargs["input"]
            return _FakeResponse(
                data=[{"embedding": [float(i)]} for i, _ in enumerate(batch)]
            )

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        monkeypatch.setattr(embed.time, "sleep", lambda s: sleeps.append(s))

        result = embed.embed_texts(["a"], model="ollama/any")
        assert result.shape == (1, 1)
        assert calls["n"] == 3
        # Exponential backoff: 5, 10, ...
        assert sleeps == [5.0, 10.0]

    def test_respects_max_retries_env_var(self, monkeypatch):
        monkeypatch.setenv("KB_EMBED_MAX_RETRIES", "2")
        sleeps: list[float] = []

        def always_rate_limited(**kwargs):
            raise self._rate_limit_error()

        monkeypatch.setattr(embed.litellm, "embedding", always_rate_limited)
        monkeypatch.setattr(embed.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(litellm.RateLimitError):
            embed.embed_texts(["a"], model="ollama/any")

        # 2 retries => 2 sleeps (5, 10), then the third attempt raises.
        assert len(sleeps) == 2

    def test_non_rate_limit_errors_fail_fast(self, monkeypatch):
        """A RuntimeError or auth error must not trigger retry — retrying
        won't help, it'll just slow the failure down."""
        calls = {"n": 0}

        def fake_embedding(**kwargs):
            calls["n"] += 1
            raise RuntimeError("boom")

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        monkeypatch.setattr(embed.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="boom"):
            embed.embed_texts(["a"], model="ollama/any")
        assert calls["n"] == 1

    def test_backoff_is_capped(self, monkeypatch):
        """Exponential backoff must cap at _MAX_BACKOFF_SECONDS so a
        pathologically-long rate-limit window doesn't produce absurd sleeps."""
        monkeypatch.setenv("KB_EMBED_MAX_RETRIES", "10")
        sleeps: list[float] = []

        def always_rate_limited(**kwargs):
            raise self._rate_limit_error()

        monkeypatch.setattr(embed.litellm, "embedding", always_rate_limited)
        monkeypatch.setattr(embed.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(litellm.RateLimitError):
            embed.embed_texts(["a"], model="ollama/any")

        assert sleeps[0] == 5.0
        # All delays must be within the cap; later ones saturate at 60.
        assert max(sleeps) == embed._MAX_BACKOFF_SECONDS
        assert all(s <= embed._MAX_BACKOFF_SECONDS for s in sleeps)


class TestEmbedQuery:
    def test_returns_single_vector(self, monkeypatch):
        def fake_embedding(**kwargs):
            return _FakeResponse(data=[{"embedding": [1.0, 2.0, 3.0]}])

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        vector = embed.embed_query("hello", model="ollama/any")
        assert vector.shape == (3,)
        np.testing.assert_array_equal(vector, np.array([1.0, 2.0, 3.0], dtype=np.float32))
