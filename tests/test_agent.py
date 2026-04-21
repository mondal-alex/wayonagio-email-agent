"""Unit tests for agent.py.

Gmail API and LLM calls are fully mocked.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from wayonagio_email_agent import agent
from wayonagio_email_agent.agent import (
    _build_references,
    _process_message,
    manual_draft_flow,
    scan_loop,
    scan_once,
)


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
    @pytest.fixture(autouse=True)
    def _stub_thread_transcript(self, monkeypatch):
        monkeypatch.setattr(
            agent.gmail_client,
            "build_thread_transcript",
            lambda **kwargs: "THREAD CONTEXT",
        )

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

        mock_gen.assert_called_once_with(
            thread_transcript="THREAD CONTEXT",
            subject=_FAKE_PARTS["subject"],
            language="es",
            latest_customer_turn=_FAKE_PARTS["body"],
        )

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

        mock_detect.assert_called_once_with("THREAD CONTEXT")

    def test_forced_language_skips_detection(self):
        with (
            patch("wayonagio_email_agent.agent.gmail_client.get_message", return_value=_FAKE_MESSAGE),
            patch("wayonagio_email_agent.agent.gmail_client.extract_message_parts", return_value=_FAKE_PARTS),
            patch("wayonagio_email_agent.agent.llm.detect_language") as mock_detect,
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Risposta") as mock_generate,
            patch("wayonagio_email_agent.agent.gmail_client.draft_reply", return_value={"id": "x"}),
        ):
            manual_draft_flow("msg-001", forced_language="es")

        mock_detect.assert_not_called()
        mock_generate.assert_called_once_with(
            thread_transcript="THREAD CONTEXT",
            subject=_FAKE_PARTS["subject"],
            language="es",
            latest_customer_turn=_FAKE_PARTS["body"],
        )


# ---------------------------------------------------------------------------
# _process_message (scanner path)
# ---------------------------------------------------------------------------

class TestProcessMessage:
    @pytest.fixture(autouse=True)
    def _stub_thread_transcript(self, monkeypatch):
        monkeypatch.setattr(
            agent.gmail_client,
            "build_thread_transcript",
            lambda **kwargs: "THREAD CONTEXT",
        )

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

        mock_generate.assert_called_once_with(
            thread_transcript="THREAD CONTEXT",
            subject=_FAKE_PARTS["subject"],
            language="en",
            latest_customer_turn=_FAKE_PARTS["subject"],
        )

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


# ---------------------------------------------------------------------------
# _build_references
# ---------------------------------------------------------------------------

class TestBuildReferences:
    def test_appends_to_existing_chain(self):
        result = _build_references("<a@x> <b@x>", "<c@x>")
        assert result == "<a@x> <b@x> <c@x>"

    def test_returns_message_id_when_chain_is_empty(self):
        assert _build_references("", "<c@x>") == "<c@x>"

    def test_collapses_surrounding_and_internal_whitespace(self):
        # The chain can come from a header with trailing whitespace OR with
        # extra internal spaces between message IDs (line-folded headers,
        # upstream reformatting). Collapse both so the outgoing References
        # line is well-formed regardless of input shape.
        result = _build_references("  <a@x>   <b@x>  ", "<c@x>")
        assert result == "<a@x> <b@x> <c@x>"


# ---------------------------------------------------------------------------
# scan_loop smoke test
# ---------------------------------------------------------------------------

class TestScanLoop:
    def test_runs_one_iteration_then_respects_keyboard_interrupt(self):
        """scan_loop must invoke scan_once and exit cleanly on Ctrl-C."""
        calls: list[bool] = []

        def fake_scan_once(dry_run: bool) -> None:
            calls.append(dry_run)

        def fake_sleep(_seconds: int) -> None:
            raise KeyboardInterrupt()

        with (
            patch("wayonagio_email_agent.agent.scan_once", side_effect=fake_scan_once),
            patch("wayonagio_email_agent.agent.time.sleep", side_effect=fake_sleep),
            pytest.raises(KeyboardInterrupt),
        ):
            scan_loop(interval=1, dry_run=True)

        assert calls == [True]

    def test_continues_after_iteration_failure(self):
        """An error in one iteration must not kill the loop."""
        call_count = {"n": 0}

        def flaky_scan_once(dry_run: bool) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient failure")
            raise KeyboardInterrupt()

        with (
            patch("wayonagio_email_agent.agent.scan_once", side_effect=flaky_scan_once),
            patch("wayonagio_email_agent.agent.time.sleep", return_value=None),
            pytest.raises(KeyboardInterrupt),
        ):
            scan_loop(interval=0, dry_run=False)

        assert call_count["n"] == 2


class TestScanOnce:
    def test_does_nothing_when_no_unread_messages(self):
        with (
            patch("wayonagio_email_agent.agent.gmail_client.list_messages", return_value=[]),
            patch("wayonagio_email_agent.agent._process_message") as mock_process,
        ):
            scan_once(dry_run=False)

        mock_process.assert_not_called()

    def test_processes_each_unread_message(self):
        with (
            patch(
                "wayonagio_email_agent.agent.gmail_client.list_messages",
                return_value=[{"id": "a"}, {"id": "b"}],
            ),
            patch("wayonagio_email_agent.agent._process_message") as mock_process,
        ):
            scan_once(dry_run=True)

        assert mock_process.call_count == 2
        mock_process.assert_any_call("a", dry_run=True)
        mock_process.assert_any_call("b", dry_run=True)

    def test_per_message_failure_does_not_abort_pass(self):
        def flaky(message_id: str, dry_run: bool) -> None:
            if message_id == "a":
                raise RuntimeError("boom")

        with (
            patch(
                "wayonagio_email_agent.agent.gmail_client.list_messages",
                return_value=[{"id": "a"}, {"id": "b"}],
            ),
            patch(
                "wayonagio_email_agent.agent._process_message",
                side_effect=flaky,
            ) as mock_process,
        ):
            scan_once(dry_run=False)

        assert mock_process.call_count == 2

    def test_kb_unavailable_on_one_message_does_not_abort_pass(self):
        """KBUnavailableError on a single message must be logged and the
        scanner must move on to the next one. Otherwise a transient KB
        outage would silently stall the entire batch.

        Anchors the "log and continue" contract explicitly for the most
        likely runtime exception now that the KB is required.
        """
        from wayonagio_email_agent.kb.retrieve import KBUnavailableError

        processed: list[str] = []

        def fake_process(message_id: str, dry_run: bool) -> None:
            processed.append(message_id)
            if message_id == "a":
                raise KBUnavailableError(
                    "KB index artifact could not be downloaded."
                )

        with (
            patch(
                "wayonagio_email_agent.agent.gmail_client.list_messages",
                return_value=[{"id": "a"}, {"id": "b"}],
            ),
            patch(
                "wayonagio_email_agent.agent._process_message",
                side_effect=fake_process,
            ),
        ):
            scan_once(dry_run=False)

        assert processed == ["a", "b"], (
            "scan_once aborted after KBUnavailableError on the first message; "
            "the batch must be resilient to per-message KB failures."
        )


# ---------------------------------------------------------------------------
# Draft-only invariant: belt-and-braces on top of the OAuth scope
# ---------------------------------------------------------------------------

class TestDraftOnlyInvariant:
    """Safety net: this service must never call ``drafts.send`` or ``messages.send``.

    The OAuth scope (``gmail.compose``, no ``gmail.send``) already guarantees
    this at Google's end. These tests bake the invariant into CI so a future
    refactor cannot silently re-introduce a send path.
    """

    def _fake_message_payload(self) -> dict:
        body_b64 = base64.urlsafe_b64encode(
            "Ciao, vorrei prenotare un tour a Cusco.".encode("utf-8")
        ).decode("ascii")
        return {
            "id": "msg-001",
            "threadId": "thread-001",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Tour inquiry"},
                    {"name": "From", "value": "guest@example.com"},
                    {"name": "Message-Id", "value": "<msg-001@example.com>"},
                ],
                "mimeType": "text/plain",
                "body": {"data": body_b64},
            },
        }

    def _build_fake_service(self) -> MagicMock:
        service = MagicMock()
        # Any attempt to *call* .send(...) will raise, and assert_not_called()
        # at the end of the test verifies it never even was accessed as a call.
        service.users().messages().send.side_effect = AssertionError(
            "messages.send was called — this service must be draft-only."
        )
        service.users().drafts().send.side_effect = AssertionError(
            "drafts.send was called — this service must be draft-only."
        )
        return service

    def test_manual_flow_never_calls_send(self):
        service = self._build_fake_service()
        fake_message = self._fake_message_payload()
        service.users().messages().get.return_value.execute.return_value = fake_message
        service.users().threads().get.return_value.execute.return_value = {
            "messages": [fake_message]
        }
        service.users().drafts().create.return_value.execute.return_value = {
            "id": "draft-abc"
        }

        with (
            patch("wayonagio_email_agent.gmail_client._build_service", return_value=service),
            patch("wayonagio_email_agent.agent.llm.detect_language", return_value="it"),
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Risposta"),
        ):
            manual_draft_flow("msg-001")

        service.users().messages().send.assert_not_called()
        service.users().drafts().send.assert_not_called()
        service.users().drafts().create.assert_called()

    def test_scanner_flow_never_calls_send(self):
        service = self._build_fake_service()
        service.users().messages().get.return_value.execute.return_value = (
            self._fake_message_payload()
        )
        service.users().threads().get.return_value.execute.return_value = {
            "messages": [{"id": "msg-001", "labelIds": ["INBOX"]}]
        }
        service.users().drafts().create.return_value.execute.return_value = {
            "id": "draft-scanner"
        }

        with (
            patch("wayonagio_email_agent.gmail_client._build_service", return_value=service),
            patch("wayonagio_email_agent.agent.state.is_processed", return_value=False),
            patch("wayonagio_email_agent.agent.state.mark_processed"),
            patch("wayonagio_email_agent.agent.llm.is_travel_related", return_value=(True, "it")),
            patch("wayonagio_email_agent.agent.llm.generate_reply", return_value="Risposta"),
        ):
            _process_message("msg-001", dry_run=False)

        service.users().messages().send.assert_not_called()
        service.users().drafts().send.assert_not_called()


# ---------------------------------------------------------------------------
# scanner_enabled
# ---------------------------------------------------------------------------

class TestScannerEnabled:
    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "ON"])
    def test_truthy_values(self, monkeypatch, value: str):
        monkeypatch.setenv("SCANNER_ENABLED", value)
        assert agent.scanner_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "off"])
    def test_falsy_values(self, monkeypatch, value: str):
        monkeypatch.setenv("SCANNER_ENABLED", value)
        assert agent.scanner_enabled() is False

    def test_defaults_to_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("SCANNER_ENABLED", raising=False)
        assert agent.scanner_enabled() is False
