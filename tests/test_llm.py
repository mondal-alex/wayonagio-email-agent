"""Unit tests for llm/client.py.

All LLM network calls are mocked via unittest.mock so no real LLM provider
(Ollama server or Gemini API) is required.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wayonagio_email_agent.llm.client import (
    EmptyReplyError,
    _build_kwargs,
    _chat,
    detect_language,
    generate_reply,
    is_travel_related,
)


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

class TestDetectLanguage:
    def test_returns_it(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="it"):
            assert detect_language("Ciao, vorrei prenotare un tour") == "it"

    def test_returns_es(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="es"):
            assert detect_language("Hola, quisiera reservar un tour") == "es"

    def test_returns_en(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="en"):
            assert detect_language("Hello, I would like to book a tour") == "en"

    def test_defaults_to_en_on_unknown(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="fr"):
            assert detect_language("Bonjour") == "en"

    def test_extracts_code_from_verbose_response(self):
        with patch(
            "wayonagio_email_agent.llm.client._chat",
            return_value="The language is: it",
        ):
            assert detect_language("Ciao") == "it"

    def test_does_not_match_language_code_inside_words(self):
        with patch(
            "wayonagio_email_agent.llm.client._chat",
            return_value="This is limited context.",
        ):
            assert detect_language("Hello") == "en"

    def test_code_is_stripped_of_trailing_punctuation(self):
        with patch(
            "wayonagio_email_agent.llm.client._chat",
            return_value="it.",
        ):
            assert detect_language("Ciao") == "it"

    def test_only_parses_first_line(self):
        """If the LLM answers on line 1 and rambles after, trust line 1."""
        with patch(
            "wayonagio_email_agent.llm.client._chat",
            return_value="en\nbecause the text contains Italian and Spanish words",
        ):
            assert detect_language("mixed text") == "en"


# ---------------------------------------------------------------------------
# generate_reply
# ---------------------------------------------------------------------------

class TestGenerateReply:
    def test_returns_reply_text(self):
        expected = "Gentile cliente, grazie per la sua richiesta."
        with patch("wayonagio_email_agent.llm.client._chat", return_value=expected):
            result = generate_reply("Vorrei informazioni sui tour", "it")
        assert result == expected

    def test_language_is_included_in_prompt(self):
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply("Hello, I need info", "en")
            call_args = mock_chat.call_args[0][0]  # list of messages
            user_msg = next(m for m in call_args if m["role"] == "user")
            assert "English" in user_msg["content"]

    def test_spanish_label_in_prompt(self):
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "respuesta"
            generate_reply("Hola", "es")
            call_args = mock_chat.call_args[0][0]
            user_msg = next(m for m in call_args if m["role"] == "user")
            assert "Spanish" in user_msg["content"]

    def test_empty_reply_raises_rather_than_drafting_blank(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value=""):
            with pytest.raises(EmptyReplyError):
                generate_reply("Ciao", "it")

    def test_whitespace_only_reply_raises(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="   \n  \t"):
            with pytest.raises(EmptyReplyError):
                generate_reply("Ciao", "it")

    def test_forwards_generous_max_tokens_to_chat(self):
        """Regression: ensure the token cap is not silently lowered to a value
        that would truncate a polite multi-paragraph travel reply."""
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply("Ciao", "it")

        _, kwargs = mock_chat.call_args
        assert kwargs["options"]["max_tokens"] >= 800


# ---------------------------------------------------------------------------
# KB augmentation (optional, gated by KB_ENABLED)
# ---------------------------------------------------------------------------

class TestGenerateReplyKBIntegration:
    """Wire-up tests: the KB must augment the prompt when enabled, and never
    block drafting when KB calls fail."""

    def test_prompt_unchanged_when_kb_disabled(self, monkeypatch):
        monkeypatch.setenv("KB_ENABLED", "false")
        from wayonagio_email_agent.kb import retrieve as kb_retrieve

        kb_retrieve.reset_cache()

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply("Hello", "en")

        messages = mock_chat.call_args[0][0]
        user = next(m for m in messages if m["role"] == "user")["content"]
        assert "REFERENCE MATERIAL" not in user

    def test_retrieved_chunks_are_injected_into_user_prompt(self, monkeypatch):
        from wayonagio_email_agent.kb import retrieve as kb_retrieve
        from wayonagio_email_agent.kb.store import ScoredChunk

        chunk = ScoredChunk(
            text="Machu Picchu tour costs $250/person.",
            source_id="sid",
            source_name="MachuPicchu.md",
            source_path="Tours / MachuPicchu.md",
            chunk_index=0,
            score=0.93,
        )
        monkeypatch.setattr(kb_retrieve, "retrieve", lambda q, top_k=None: [chunk])

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply("How much is Machu Picchu?", "en")

        messages = mock_chat.call_args[0][0]
        user = next(m for m in messages if m["role"] == "user")["content"]
        assert "REFERENCE MATERIAL" in user
        assert "Tours / MachuPicchu.md" in user
        assert "Machu Picchu tour costs $250/person." in user
        assert "USE OF REFERENCE MATERIAL" in user

    def test_kb_failures_do_not_block_drafting(self, monkeypatch):
        from wayonagio_email_agent.kb import retrieve as kb_retrieve

        def boom(*_a, **_kw):
            raise RuntimeError("KB down")

        monkeypatch.setattr(kb_retrieve, "retrieve", boom)

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            result = generate_reply("Hello", "en")

        assert result == "reply"


# ---------------------------------------------------------------------------
# _chat: truncation warning
# ---------------------------------------------------------------------------

def _fake_litellm_response(content: str, finish_reason: str | None) -> SimpleNamespace:
    """Build a minimal object that looks like a LiteLLM ChatCompletion."""
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content),
        finish_reason=finish_reason,
    )
    return SimpleNamespace(choices=[choice])


class TestChatTruncationWarning:
    def test_warns_when_finish_reason_is_length(self, monkeypatch, caplog):
        monkeypatch.setenv("LLM_MODEL", "ollama/llama3.2")
        with caplog.at_level(logging.WARNING, logger="wayonagio_email_agent.llm.client"):
            with patch(
                "wayonagio_email_agent.llm.client.litellm.completion",
                return_value=_fake_litellm_response("partial reply", "length"),
            ):
                _chat([{"role": "user", "content": "hi"}])

        messages = [r.getMessage() for r in caplog.records]
        assert any("truncated" in m.lower() for m in messages)

    def test_does_not_warn_on_normal_stop(self, monkeypatch, caplog):
        monkeypatch.setenv("LLM_MODEL", "ollama/llama3.2")
        with caplog.at_level(logging.WARNING, logger="wayonagio_email_agent.llm.client"):
            with patch(
                "wayonagio_email_agent.llm.client.litellm.completion",
                return_value=_fake_litellm_response("complete reply", "stop"),
            ):
                _chat([{"role": "user", "content": "hi"}])

        messages = [r.getMessage() for r in caplog.records]
        assert not any("truncated" in m.lower() for m in messages)


# ---------------------------------------------------------------------------
# is_travel_related
# ---------------------------------------------------------------------------

class TestIsTravelRelated:
    def test_travel_related_italian(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="yes it"):
            related, lang = is_travel_related("Tour Machu Picchu", "Vorrei un tour")
        assert related is True
        assert lang == "it"

    def test_not_travel_related(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="no en"):
            related, lang = is_travel_related("Invoice #123", "Please find attached")
        assert related is False
        assert lang == "en"

    def test_travel_related_spanish(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="yes es"):
            related, lang = is_travel_related("Cusco tour", "Precio para grupo")
        assert related is True
        assert lang == "es"

    def test_defaults_language_to_en_when_missing(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="yes"):
            related, lang = is_travel_related("Tour", "Hello")
        assert related is True
        assert lang == "en"

    def test_llm_error_propagates(self):
        with patch(
            "wayonagio_email_agent.llm.client._chat",
            side_effect=ConnectionError("LLM not reachable"),
        ):
            with pytest.raises(ConnectionError):
                is_travel_related("subject", "body")


# ---------------------------------------------------------------------------
# _build_kwargs: provider routing (LiteLLM-specific)
# ---------------------------------------------------------------------------

class TestBuildKwargsProviderRouting:
    """Verify that provider-specific config is honored without calling LiteLLM."""

    def test_ollama_routing_with_legacy_env(self, monkeypatch):
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:1b")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.local:11434")
        monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "30m")

        kwargs = _build_kwargs([{"role": "user", "content": "hi"}], None)

        assert kwargs["model"] == "ollama/llama3.2:1b"
        assert kwargs["api_base"] == "http://ollama.local:11434"
        assert kwargs["keep_alive"] == "30m"
        assert "api_key" not in kwargs

    def test_ollama_routing_with_llm_model(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "ollama/mistral")
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)

        kwargs = _build_kwargs([{"role": "user", "content": "hi"}], None)

        assert kwargs["model"] == "ollama/mistral"
        assert kwargs["api_base"] == "http://localhost:11434"
        assert kwargs["keep_alive"] == "1h"

    def test_gemini_routing_requires_api_key(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gemini/gemini-2.5-flash")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            _build_kwargs([{"role": "user", "content": "hi"}], None)

    def test_gemini_routing_uses_api_key(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gemini/gemini-2.5-flash")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")

        kwargs = _build_kwargs([{"role": "user", "content": "hi"}], None)

        assert kwargs["model"] == "gemini/gemini-2.5-flash"
        assert kwargs["api_key"] == "test-key-123"
        assert "api_base" not in kwargs
        assert "keep_alive" not in kwargs

    def test_options_are_forwarded(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "ollama/llama3.2")

        kwargs = _build_kwargs(
            [{"role": "user", "content": "hi"}],
            {"temperature": 0.4, "max_tokens": 350},
        )

        assert kwargs["temperature"] == 0.4
        assert kwargs["max_tokens"] == 350
