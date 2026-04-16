"""Unit tests for llm/ollama.py.

All Ollama network calls are mocked via unittest.mock so no real Ollama
server is required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from wayonagio_email_agent.llm.ollama import (
    detect_language,
    generate_reply,
    is_travel_related,
)


def _make_response(content: str) -> SimpleNamespace:
    """Build a minimal mock ollama response."""
    return SimpleNamespace(message=SimpleNamespace(content=content))


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

class TestDetectLanguage:
    def test_returns_it(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="it"):
            assert detect_language("Ciao, vorrei prenotare un tour") == "it"

    def test_returns_es(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="es"):
            assert detect_language("Hola, quisiera reservar un tour") == "es"

    def test_returns_en(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="en"):
            assert detect_language("Hello, I would like to book a tour") == "en"

    def test_defaults_to_en_on_unknown(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="fr"):
            assert detect_language("Bonjour") == "en"

    def test_extracts_code_from_verbose_response(self):
        with patch(
            "wayonagio_email_agent.llm.ollama._chat",
            return_value="The language is: it",
        ):
            assert detect_language("Ciao") == "it"

    def test_does_not_match_language_code_inside_words(self):
        with patch(
            "wayonagio_email_agent.llm.ollama._chat",
            return_value="This is limited context.",
        ):
            assert detect_language("Hello") == "en"


# ---------------------------------------------------------------------------
# generate_reply
# ---------------------------------------------------------------------------

class TestGenerateReply:
    def test_returns_reply_text(self):
        expected = "Gentile cliente, grazie per la sua richiesta."
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value=expected):
            result = generate_reply("Vorrei informazioni sui tour", "it")
        assert result == expected

    def test_language_is_included_in_prompt(self):
        with patch("wayonagio_email_agent.llm.ollama._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply("Hello, I need info", "en")
            call_args = mock_chat.call_args[0][0]  # list of messages
            system_msg = next(m for m in call_args if m["role"] == "user")
            assert "English" in system_msg["content"]

    def test_spanish_label_in_prompt(self):
        with patch("wayonagio_email_agent.llm.ollama._chat") as mock_chat:
            mock_chat.return_value = "respuesta"
            generate_reply("Hola", "es")
            call_args = mock_chat.call_args[0][0]
            user_msg = next(m for m in call_args if m["role"] == "user")
            assert "Spanish" in user_msg["content"]


# ---------------------------------------------------------------------------
# is_travel_related
# ---------------------------------------------------------------------------

class TestIsTravelRelated:
    def test_travel_related_italian(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="yes it"):
            related, lang = is_travel_related("Tour Machu Picchu", "Vorrei un tour")
        assert related is True
        assert lang == "it"

    def test_not_travel_related(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="no en"):
            related, lang = is_travel_related("Invoice #123", "Please find attached")
        assert related is False
        assert lang == "en"

    def test_travel_related_spanish(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="yes es"):
            related, lang = is_travel_related("Cusco tour", "Precio para grupo")
        assert related is True
        assert lang == "es"

    def test_defaults_language_to_en_when_missing(self):
        with patch("wayonagio_email_agent.llm.ollama._chat", return_value="yes"):
            related, lang = is_travel_related("Tour", "Hello")
        assert related is True
        assert lang == "en"

    def test_ollama_error_propagates(self):
        with patch(
            "wayonagio_email_agent.llm.ollama._chat",
            side_effect=ConnectionError("Ollama not running"),
        ):
            with pytest.raises(ConnectionError):
                is_travel_related("subject", "body")
