"""Unit tests for llm/client.py.

All LLM network calls are mocked via unittest.mock so no real LLM provider
(Ollama server or Gemini API) is required.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from wayonagio_email_agent.llm.client import (
    EmptyReplyError,
    _build_kwargs,
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
