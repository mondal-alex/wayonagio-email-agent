"""Unit tests for kb/embed.py."""

from __future__ import annotations

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


class TestEmbedQuery:
    def test_returns_single_vector(self, monkeypatch):
        def fake_embedding(**kwargs):
            return _FakeResponse(data=[{"embedding": [1.0, 2.0, 3.0]}])

        monkeypatch.setattr(embed.litellm, "embedding", fake_embedding)
        vector = embed.embed_query("hello", model="ollama/any")
        assert vector.shape == (3,)
        np.testing.assert_array_equal(vector, np.array([1.0, 2.0, 3.0], dtype=np.float32))
