"""Unit tests for agent.py.

Gmail API and LLM calls are fully mocked.
"""

from __future__ import annotations

from unittest.mock import patch

from wayonagio_email_agent.agent import _process_message, manual_draft_flow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_PARTS = {
    "subject": "Tour inquiry",
    "from_": "guest@example.com",
    "to": "info@wayonagio.com",
    "body": "Ciao, vorrei prenotare un tour a Cusco.",
    "thread_id": "thread-001",
    "message_id_header": "<msg-001@mail.example.com>",
    "references": "",
}

_FAKE_MESSAGE = {"id": "msg-001", "threadId": "thread-001", "payload": {}}

# ---------------------------------------------------------------------------
# manual_draft_flow
# ---------------------------------------------------------------------------

class TestManualDraftFlow:
    def test_creates_draft(self):
        with (
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.detect_language", return_value="it"),
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Risposta di prova"),
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply", return_value={"id": "draft-999"}) as mock_draft,
        ):
            result = manual_draft_flow("msg-001")

        assert result["id"] == "draft-999"
        mock_draft.assert_called_once()
        _, kwargs = mock_draft.call_args
        assert kwargs["thread_id"] == "thread-001"
        assert kwargs["to"] == "guest@example.com"

    def test_passes_language_to_generate_reply(self):
        with (
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.detect_language", return_value="es"),
            patch("wayonagio_email_agent.agent.llm.generate_reply") as mock_gen,
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply", return_value={"id": "x"}),
        ):
            mock_gen.return_value = "Respuesta"
            manual_draft_flow("msg-001")

        mock_gen.assert_called_once_with(original=_FAKE_PARTS["body"], language="es")

    def test_uses_subject_when_body_empty(self):
        parts_no_body = {**_FAKE_PARTS, "body": ""}
        with (
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=parts_no_body),
            patch("wayonagio_email_agent.agent.llm.detect_language", return_value="en") as mock_detect,
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Reply"),
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply", return_value={"id": "x"}),
        ):
            manual_draft_flow("msg-001")

        # detect_language called with subject (fallback) not empty body
        mock_detect.assert_called_once_with(_FAKE_PARTS["subject"])


# ---------------------------------------------------------------------------
# _process_message (scanner path)
# ---------------------------------------------------------------------------

class TestProcessMessage:
    def test_skips_already_processed(self):
        with (
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=True),
            patch("wayonagio_email_agent.agent.gmail_client.get_message") as mock_get,
        ):
            _process_message("msg-001", dry_run=False)

        mock_get.assert_not_called()

    def test_skips_non_travel(self):
        with (
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=False),
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.is_travel_related", return_value=(False, "en")),
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply") as mock_draft,
            patch("wayonagio_email_agent.agent.state.mark_processed") as mock_mark,
        ):
            _process_message("msg-001", dry_run=False)

        mock_draft.assert_not_called()
        mock_mark.assert_called_once_with("msg-001", outcome="non_travel")

    def test_skips_thread_with_existing_draft(self):
        with (
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=False),
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.is_travel_related", return_value=(True, "it")),
            patch("wayonagio_email_agent.agent.gmail_client.thread_has_draft", return_value=True),
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply") as mock_draft,
            patch("wayonagio_email_agent.agent.state.mark_processed") as mock_mark,
        ):
            _process_message("msg-001", dry_run=False)

        mock_draft.assert_not_called()
        mock_mark.assert_called_once_with("msg-001", outcome="thread_has_draft")

    def test_creates_draft_and_marks_processed(self):
        with (
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=False),
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.is_travel_related", return_value=(True, "it")),
            patch("wayonagio_email_agent.agent.gmail_client.thread_has_draft", return_value=False),
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Risposta"),
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply", return_value={"id": "d1"}) as mock_draft,
            patch("wayonagio_email_agent.agent.state.mark_processed") as mock_mark,
        ):
            _process_message("msg-001", dry_run=False)

        mock_draft.assert_called_once()
        mock_mark.assert_called_once_with("msg-001", outcome="drafted")

    def test_dry_run_does_not_create_draft(self):
        with (
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=False),
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.is_travel_related", return_value=(True, "it")),
            patch("wayonagio_email_agent.agent.gmail_client.thread_has_draft", return_value=False),
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Risposta"),
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply") as mock_draft,
            patch("wayonagio_email_agent.agent.state.mark_processed") as mock_mark,
        ):
            _process_message("msg-001", dry_run=True)

        mock_draft.assert_not_called()
        mock_mark.assert_not_called()

    def test_uses_subject_when_body_empty(self):
        parts_no_body = {**_FAKE_PARTS, "body": ""}
        with (
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=False),
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=parts_no_body),
            patch("wayonagio_email_agent.agent.llm.is_travel_related", return_value=(True, "en")),
            patch("wayonagio_email_agent.agent.gmail_client.thread_has_draft", return_value=False),
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Reply") as mock_generate,
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply", return_value={"id": "d1"}),
            patch("wayonagio_email_agent.agent.state.mark_processed"),
        ):
            _process_message("msg-001", dry_run=False)

        mock_generate.assert_called_once_with(original=_FAKE_PARTS["subject"], language="en")

    def test_draft_lookup_error_does_not_create_draft(self):
        with (
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=False),
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.is_travel_related", return_value=(True, "it")),
            patch(
                "wayonagio_email_agent.agent.gmail_client.thread_has_draft",
                side_effect=RuntimeError("draft lookup failed"),
            ),
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply") as mock_draft,
            patch("wayonagio_email_agent.agent.state.mark_processed") as mock_mark,
        ):
            try:
                _process_message("msg-001", dry_run=False)
            except RuntimeError:
                pass

        mock_draft.assert_not_called()
        mock_mark.assert_not_called()
