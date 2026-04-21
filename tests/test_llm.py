"""Unit tests for llm/client.py.

All LLM network calls are mocked via unittest.mock so no real LLM provider
(Ollama server or Gemini API) is required.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import litellm
import pytest

from wayonagio_email_agent.llm import client as llm_client
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
    @pytest.fixture(autouse=True)
    def _stub_kb_and_exemplars(self, monkeypatch):
        """Stub both load-bearing dependencies so these tests focus on the
        LLM side of ``generate_reply``:

        * KB is required at runtime — without a stub, retrieval would try
          to download a non-existent index.
        * Exemplars are optional and graceful, but the loader maintains a
          process-level cache. Default to ``[]`` so most tests don't have
          to think about the EXAMPLE RESPONSES block; tests that need it
          override the loader explicitly.
        """
        from wayonagio_email_agent.exemplars import loader as exemplar_loader
        from wayonagio_email_agent.kb import retrieve as kb_retrieve

        monkeypatch.setattr(kb_retrieve, "retrieve", lambda q, top_k=None: [])
        monkeypatch.setattr(exemplar_loader, "get_all_exemplars", lambda: [])

    def test_returns_reply_text(self):
        expected = "Gentile cliente, grazie per la sua richiesta."
        with patch("wayonagio_email_agent.llm.client._chat", return_value=expected):
            result = generate_reply(
                thread_transcript="Vorrei informazioni sui tour",
                subject="Tour inquiry",
                language="it",
            )
        assert result == expected

    def test_language_is_included_in_prompt(self):
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="Hello, I need info",
                subject="Info request",
                language="en",
            )
            call_args = mock_chat.call_args[0][0]  # list of messages
            user_msg = next(m for m in call_args if m["role"] == "user")
            assert "English" in user_msg["content"]

    def test_spanish_label_in_prompt(self):
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "respuesta"
            generate_reply(
                thread_transcript="Hola",
                subject="Consulta",
                language="es",
            )
            call_args = mock_chat.call_args[0][0]
            user_msg = next(m for m in call_args if m["role"] == "user")
            assert "Spanish" in user_msg["content"]

    def test_prompt_scopes_reply_to_latest_customer_turn(self):
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="OLD: Question about refunds\nOLD: Answer already given",
                subject="Logistics",
                language="it",
                latest_customer_turn="NEW: Will the bus take us directly to the hotel?",
            )
            call_args = mock_chat.call_args[0][0]
            user_msg = next(m for m in call_args if m["role"] == "user")
            content = user_msg["content"]
            assert "RESPONSE SCOPE" in content
            assert "LATEST CUSTOMER TURN (primary task)" in content
            assert "NEW: Will the bus take us directly to the hotel?" in content
            assert "Do NOT re-answer questions" in content

    def test_empty_reply_raises_rather_than_drafting_blank(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value=""):
            with pytest.raises(EmptyReplyError):
                generate_reply(
                    thread_transcript="Ciao",
                    subject="Ciao",
                    language="it",
                )

    def test_whitespace_only_reply_raises(self):
        with patch("wayonagio_email_agent.llm.client._chat", return_value="   \n  \t"):
            with pytest.raises(EmptyReplyError):
                generate_reply(
                    thread_transcript="Ciao",
                    subject="Ciao",
                    language="it",
                )

    def test_forwards_generous_max_tokens_to_chat(self):
        """Regression: ensure the token cap is not silently lowered to a value
        that would truncate a polite multi-paragraph travel reply (or burn the
        whole budget on Gemini 2.5 internal thinking)."""
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="Ciao",
                subject="Ciao",
                language="it",
            )

        _, kwargs = mock_chat.call_args
        assert kwargs["options"]["max_tokens"] >= 4096

    def test_reply_max_tokens_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_REPLY_TOKENS", "2048")
        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="Ciao",
                subject="Ciao",
                language="it",
            )
        _, kwargs = mock_chat.call_args
        assert kwargs["options"]["max_tokens"] == 2048


# ---------------------------------------------------------------------------
# KB augmentation (optional, gated by KB_ENABLED)
# ---------------------------------------------------------------------------

class TestGenerateReplyKBIntegration:
    """Wire-up tests: the KB is required, must augment the prompt with hits,
    and KB failures must block drafting (rather than silently producing an
    ungrounded reply that staff might send unmodified)."""

    @pytest.fixture(autouse=True)
    def _stub_exemplars(self, monkeypatch):
        from wayonagio_email_agent.exemplars import loader as exemplar_loader

        monkeypatch.setattr(exemplar_loader, "get_all_exemplars", lambda: [])

    def test_retrieved_chunks_are_injected_into_user_prompt(self, monkeypatch):
        from wayonagio_email_agent.kb import retrieve as kb_retrieve
        from wayonagio_email_agent.kb.store import ScoredChunk

        captured_query: dict[str, str] = {}
        chunk = ScoredChunk(
            text="Machu Picchu tour costs $250/person.",
            source_id="sid",
            source_name="MachuPicchu.md",
            source_path="Tours / MachuPicchu.md",
            chunk_index=0,
            score=0.93,
        )

        def fake_retrieve(query: str, top_k=None):
            captured_query["value"] = query
            return [chunk]

        monkeypatch.setattr(kb_retrieve, "retrieve", fake_retrieve)

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="How much is Machu Picchu?",
                subject="Machu Picchu",
                language="en",
            )

        messages = mock_chat.call_args[0][0]
        user = next(m for m in messages if m["role"] == "user")["content"]
        assert "Subject: Machu Picchu" in captured_query["value"]
        assert "Latest customer turn:" in captured_query["value"]
        assert "Recent thread context" in captured_query["value"]
        assert "REFERENCE MATERIAL" in user
        assert "Tours / MachuPicchu.md" in user
        assert "Machu Picchu tour costs $250/person." in user
        assert "USE OF REFERENCE MATERIAL" in user

    def test_kb_failures_block_drafting(self, monkeypatch):
        """Refusing to draft is preferable to drafting an ungrounded reply."""
        from wayonagio_email_agent.kb import retrieve as kb_retrieve

        def boom(*_a, **_kw):
            raise kb_retrieve.KBUnavailableError("KB down")

        monkeypatch.setattr(kb_retrieve, "retrieve", boom)

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            with pytest.raises(kb_retrieve.KBUnavailableError):
                generate_reply(
                    thread_transcript="Hello",
                    subject="Hello",
                    language="en",
                )

        mock_chat.assert_not_called()


class TestGenerateReplyExemplarIntegration:
    """Wire-up tests for the exemplars side of the prompt. The KB is the
    hard requirement; exemplars are the optional, graceful style layer.

    The plan calls out two contracts that must hold simultaneously:

    1. When exemplars are present, the EXAMPLE RESPONSES block lands AFTER
       the REFERENCE MATERIAL block in the prompt — the framing inside the
       block says "the REFERENCE MATERIAL above is authoritative", which
       only reads correctly if the order on the page matches the words.
    2. Exemplar load failures must NOT block drafting. The loader is
       contracted to return ``[]`` on failure; if a future bug lets an
       exception escape, drafting still degrades to KB-only rather than
       breaking entirely.
    """

    @pytest.fixture(autouse=True)
    def _stub_kb_with_hit(self, monkeypatch):
        """Provide a real KB hit so we can assert ordering of the two blocks."""
        from wayonagio_email_agent.kb import retrieve as kb_retrieve
        from wayonagio_email_agent.kb.store import ScoredChunk

        chunk = ScoredChunk(
            text="Salkantay trek is 4 days, $480 per person.",
            source_id="sid",
            source_name="Salkantay.md",
            source_path="Tours / Salkantay.md",
            chunk_index=0,
            score=0.91,
        )
        monkeypatch.setattr(kb_retrieve, "retrieve", lambda q, top_k=None: [chunk])

    def test_exemplar_block_is_injected_after_reference_material(self, monkeypatch):
        from wayonagio_email_agent.exemplars import loader as exemplar_loader
        from wayonagio_email_agent.exemplars.source import Exemplar

        monkeypatch.setattr(
            exemplar_loader,
            "get_all_exemplars",
            lambda: [
                Exemplar(
                    title="Refund policy",
                    text="Hi, thank you for your message about cancellations.",
                    source_id="ex1",
                )
            ],
        )

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="Can I get a refund?",
                subject="Refund",
                language="en",
            )

        user = next(
            m for m in mock_chat.call_args[0][0] if m["role"] == "user"
        )["content"]

        assert "REFERENCE MATERIAL" in user
        assert "EXAMPLE RESPONSES" in user
        assert "Refund policy" in user
        assert "Hi, thank you for your message" in user

        # Ordering contract — REFERENCE before EXAMPLE.
        ref_idx = user.index("--- REFERENCE MATERIAL ---")
        ex_idx = user.index("--- EXAMPLE RESPONSES ---")
        client_idx = user.index("CLIENT EMAIL THREAD")
        assert ref_idx < ex_idx < client_idx, (
            "Prompt block ordering must be REFERENCE → EXAMPLE → CLIENT EMAIL THREAD"
        )

        # KB-precedence framing must be present whenever exemplars are.
        assert "if an example contradicts it, follow the REFERENCE MATERIAL" in user

    def test_no_exemplar_block_when_loader_returns_empty(self, monkeypatch):
        from wayonagio_email_agent.exemplars import loader as exemplar_loader

        monkeypatch.setattr(exemplar_loader, "get_all_exemplars", lambda: [])

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="Hello",
                subject="Hello",
                language="en",
            )

        user = next(
            m for m in mock_chat.call_args[0][0] if m["role"] == "user"
        )["content"]
        assert "EXAMPLE RESPONSES" not in user
        # KB block still present — exemplars-off must not affect KB wiring.
        assert "REFERENCE MATERIAL" in user

    def test_no_exemplar_block_when_kb_returns_no_hits(self, monkeypatch):
        """Coherence guard: the EXAMPLE RESPONSES block's framing reads
        "REFERENCE MATERIAL above is authoritative", which is meaningless
        when the KB returned no hits and there's no REFERENCE MATERIAL
        block in the prompt. Exemplars without KB grounding are also
        dangerous (model may copy example facts unmoored), so we
        additionally suppress exemplars whenever the KB had nothing
        relevant. KB is required, so this branch is the pathological
        edge-case path (``top_k=0`` / empty index), but the prompt must
        stay coherent regardless.
        """
        from wayonagio_email_agent.exemplars import loader as exemplar_loader
        from wayonagio_email_agent.exemplars.source import Exemplar
        from wayonagio_email_agent.kb import retrieve as kb_retrieve

        # Override the autouse KB stub from above to return [] this time.
        monkeypatch.setattr(kb_retrieve, "retrieve", lambda q, top_k=None: [])
        monkeypatch.setattr(
            exemplar_loader,
            "get_all_exemplars",
            lambda: [Exemplar(title="t", text="b", source_id="x")],
        )

        with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
            mock_chat.return_value = "reply"
            generate_reply(
                thread_transcript="Hello",
                subject="Hello",
                language="en",
            )

        user = next(
            m for m in mock_chat.call_args[0][0] if m["role"] == "user"
        )["content"]
        assert "REFERENCE MATERIAL" not in user
        assert "EXAMPLE RESPONSES" not in user, (
            "Exemplars must not appear when no REFERENCE MATERIAL block "
            "is in the prompt — the framing depends on it being present."
        )

    def test_exemplar_loader_exception_does_not_block_drafting(
        self, monkeypatch, caplog
    ):
        """Defensive contract: ``loader.get_all_exemplars`` is supposed to
        never raise, but the LLM client wraps the call anyway so that even
        a future regression in the loader's safety net can't take down the
        (working, KB-grounded) draft path. Exemplars are optional and
        graceful — that promise is enforced at both layers.

        When a (mocked) loader raises, ``generate_reply`` must:

        * log a WARNING that names the contract violation,
        * fall back to an empty exemplar block,
        * still call the LLM and return the reply.
        """
        from wayonagio_email_agent.exemplars import loader as exemplar_loader

        monkeypatch.setattr(
            exemplar_loader,
            "get_all_exemplars",
            lambda: (_ for _ in ()).throw(RuntimeError("loader contract broken")),
        )

        with caplog.at_level(
            logging.WARNING, logger="wayonagio_email_agent.llm.client"
        ):
            with patch("wayonagio_email_agent.llm.client._chat") as mock_chat:
                mock_chat.return_value = "reply"
                result = generate_reply(
                    thread_transcript="Hello",
                    subject="Hello",
                    language="en",
                )

        assert result == "reply"
        # KB block still present, exemplar block omitted entirely.
        user = next(
            m for m in mock_chat.call_args[0][0] if m["role"] == "user"
        )["content"]
        assert "REFERENCE MATERIAL" in user
        assert "EXAMPLE RESPONSES" not in user

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "never-raises contract" in m for m in messages
        ), "expected a WARNING naming the loader contract violation"


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
        assert any("LLM_MAX_REPLY_TOKENS" in m for m in messages)

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


class TestChatTransientRetry:
    """Gemini intermittently returns 503 UNAVAILABLE (capacity); we must retry."""

    def test_recovers_from_service_unavailable(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gemini/gemini-2.5-flash")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        calls = {"n": 0}
        sleeps: list[float] = []

        def fake_completion(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise litellm.ServiceUnavailableError(
                    "503 UNAVAILABLE", "gemini", "gemini/gemini-2.5-flash"
                )
            return _fake_litellm_response("complete reply", "stop")

        monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)
        monkeypatch.setattr(llm_client.time, "sleep", lambda s: sleeps.append(s))

        out = _chat([{"role": "user", "content": "hi"}])
        assert out == "complete reply"
        assert calls["n"] == 3
        assert sleeps == [3.0, 6.0]

    def test_respects_llm_chat_max_retries_env(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gemini/gemini-2.5-flash")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_CHAT_MAX_RETRIES", "1")
        sleeps: list[float] = []

        def always_503(**kwargs):
            raise litellm.ServiceUnavailableError(
                "503", "gemini", "gemini/gemini-2.5-flash"
            )

        monkeypatch.setattr(llm_client.litellm, "completion", always_503)
        monkeypatch.setattr(llm_client.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(litellm.ServiceUnavailableError):
            _chat([{"role": "user", "content": "hi"}])

        assert len(sleeps) == 1

    def test_non_transient_errors_fail_fast(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gemini/gemini-2.5-flash")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        calls = {"n": 0}

        def boom(**kwargs):
            calls["n"] += 1
            raise RuntimeError("not transient")

        monkeypatch.setattr(llm_client.litellm, "completion", boom)
        monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="not transient"):
            _chat([{"role": "user", "content": "hi"}])
        assert calls["n"] == 1


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
