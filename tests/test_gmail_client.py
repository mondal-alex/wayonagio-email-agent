"""Unit tests for gmail_client.py."""

from __future__ import annotations

import base64
from email import message_from_bytes
from unittest.mock import Mock, patch

import pytest
from googleapiclient.errors import HttpError

from wayonagio_email_agent import gmail_client


def _urlsafe_b64(text: str, *, strip_padding: bool = False) -> str:
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=") if strip_padding else encoded


class TestDraftReply:
    def test_builds_reply_mime_and_thread_metadata(self):
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value
        drafts_resource.create.return_value.execute.return_value = {"id": "draft-123"}

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            draft = gmail_client.draft_reply(
                thread_id="thread-1",
                to="guest@example.com",
                subject="Machu Picchu inquiry",
                body="Thanks for reaching out.",
                in_reply_to="<msg@example.com>",
                references="<ref-1@example.com> <msg@example.com>",
            )

        assert draft == {"id": "draft-123"}
        _, kwargs = drafts_resource.create.call_args
        assert kwargs["userId"] == "me"
        assert kwargs["body"]["message"]["threadId"] == "thread-1"

        raw = kwargs["body"]["message"]["raw"]
        mime = message_from_bytes(base64.urlsafe_b64decode(raw))
        assert mime["To"] == "guest@example.com"
        assert mime["Subject"] == "Re: Machu Picchu inquiry"
        assert mime["In-Reply-To"] == "<msg@example.com>"
        assert mime["References"] == "<ref-1@example.com> <msg@example.com>"
        assert mime.get_payload(decode=True).decode("utf-8") == "Thanks for reaching out."

    def test_preserves_existing_reply_prefix(self):
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value
        drafts_resource.create.return_value.execute.return_value = {"id": "draft-123"}

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            gmail_client.draft_reply(
                thread_id="thread-1",
                to="guest@example.com",
                subject="Re: Existing thread",
                body="Reply body",
                in_reply_to="<msg@example.com>",
                references="<msg@example.com>",
            )

        raw = drafts_resource.create.call_args.kwargs["body"]["message"]["raw"]
        mime = message_from_bytes(base64.urlsafe_b64decode(raw))
        assert mime["Subject"] == "Re: Existing thread"

    def test_non_ascii_subject_and_body_round_trip_utf8(self):
        """Italian / Spanish / emoji content must survive the MIME round-trip.

        Guards against someone "simplifying" MIMEText back to plain ASCII and
        silently mangling client mail in production.
        """
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value
        drafts_resource.create.return_value.execute.return_value = {"id": "draft-u8"}

        subject = "Prenotazione tour a Machu Picchu — café e reservación ✈️"
        body = (
            "Gentile cliente, grazie per la richiesta!\n"
            "Le inviamo il preventivo per il tour a Machu Picchu. "
            "Se desidera aggiungere il pranzo tradizionale (con ají), ci faccia sapere.\n"
            "— Wayonagio"
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            gmail_client.draft_reply(
                thread_id="thread-u8",
                to="ciente@example.com",
                subject=subject,
                body=body,
                in_reply_to="<u8@example.com>",
                references="<u8@example.com>",
            )

        raw = drafts_resource.create.call_args.kwargs["body"]["message"]["raw"]
        mime = message_from_bytes(base64.urlsafe_b64decode(raw))
        # Subject decodes correctly (MIME encodes it via RFC 2047 when needed,
        # message_from_bytes parses it back to the raw string).
        from email.header import decode_header, make_header
        decoded_subject = str(make_header(decode_header(mime["Subject"])))
        assert decoded_subject == f"Re: {subject}"
        assert mime.get_payload(decode=True).decode("utf-8") == body


class TestThreadHasDraft:
    def _threads_resource(self, service: Mock) -> Mock:
        return service.users.return_value.threads.return_value

    def test_returns_true_when_any_message_has_draft_label(self):
        service = Mock()
        self._threads_resource(service).get.return_value.execute.return_value = {
            "messages": [
                {"id": "m1", "labelIds": ["INBOX"]},
                {"id": "m2", "labelIds": ["DRAFT"]},
            ]
        }

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            assert gmail_client.thread_has_draft("thread-42") is True

        self._threads_resource(service).get.assert_called_once_with(
            userId="me", id="thread-42", format="metadata"
        )

    def test_returns_false_when_no_message_has_draft_label(self):
        service = Mock()
        self._threads_resource(service).get.return_value.execute.return_value = {
            "messages": [
                {"id": "m1", "labelIds": ["INBOX"]},
                {"id": "m2", "labelIds": ["INBOX", "UNREAD"]},
            ]
        }

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            assert gmail_client.thread_has_draft("thread-42") is False

    def test_returns_false_when_thread_has_no_messages(self):
        service = Mock()
        self._threads_resource(service).get.return_value.execute.return_value = {}

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            assert gmail_client.thread_has_draft("thread-42") is False

    def test_returns_false_when_message_has_no_labels(self):
        service = Mock()
        self._threads_resource(service).get.return_value.execute.return_value = {
            "messages": [{"id": "m1"}]
        }

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            assert gmail_client.thread_has_draft("thread-42") is False

    def test_raises_when_gmail_api_check_fails(self):
        service = Mock()
        self._threads_resource(service).get.return_value.execute.side_effect = HttpError(
            resp=Mock(status=500, reason="boom"),
            content=b"failed",
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            with pytest.raises(HttpError):
                gmail_client.thread_has_draft("thread-42")

    def test_is_single_api_call_regardless_of_draft_count(self):
        """Regression guard: no mailbox-wide drafts.list scan."""
        service = Mock()
        self._threads_resource(service).get.return_value.execute.return_value = {
            "messages": [{"id": "m1", "labelIds": ["DRAFT"]}]
        }

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            gmail_client.thread_has_draft("thread-42")

        # threads.get() is called once; drafts.list is never called at all.
        assert self._threads_resource(service).get.call_count == 1
        service.users.return_value.drafts.return_value.list.assert_not_called()


class TestGetMessagesMetadata:
    """Regression guards for the N+1 fix in ``cli list``.

    The old code made one ``messages.get`` HTTP call per listed message.
    The new code bundles them all into a single ``BatchHttpRequest``. These
    tests verify (a) the batch request is built with the expected parameters
    and (b) per-message fetch errors are surfaced without aborting the batch.
    """

    def _build_service_with_batch(self, captured: dict) -> Mock:
        service = Mock()
        batch = Mock()
        added: list[tuple[Mock, str]] = []

        def fake_add(request, request_id):
            added.append((request, request_id))

        batch.add.side_effect = fake_add
        captured["batch"] = batch
        captured["added"] = added
        service.new_batch_http_request = Mock(return_value=batch)
        return service

    def test_returns_empty_list_when_no_ids(self):
        with patch("wayonagio_email_agent.gmail_client._build_service") as mock_build:
            result = gmail_client.get_messages_metadata([])

        assert result == []
        mock_build.assert_not_called()

    def test_single_batch_call_for_all_ids(self):
        captured: dict = {}
        service = self._build_service_with_batch(captured)

        def run_batch():
            callback = service.new_batch_http_request.call_args.kwargs["callback"]
            callback(
                "id-1",
                {
                    "threadId": "t1",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "One"},
                            {"name": "From", "value": "a@example.com"},
                        ]
                    },
                },
                None,
            )
            callback(
                "id-2",
                {
                    "threadId": "t2",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Two"},
                            {"name": "From", "value": "b@example.com"},
                        ]
                    },
                },
                None,
            )

        captured["batch"].execute.side_effect = run_batch

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            rows = gmail_client.get_messages_metadata(["id-1", "id-2"])

        captured["batch"].execute.assert_called_once()
        assert len(captured["added"]) == 2
        assert [rid for _, rid in captured["added"]] == ["id-1", "id-2"]

        service.users().messages().get.assert_any_call(
            userId="me", id="id-1", format="metadata", metadataHeaders=["Subject", "From"]
        )

        by_id = {r["id"]: r for r in rows}
        assert by_id["id-1"]["subject"] == "One"
        assert by_id["id-1"]["from_"] == "a@example.com"
        assert by_id["id-2"]["subject"] == "Two"
        assert by_id["id-2"]["from_"] == "b@example.com"

    def test_per_message_error_is_surfaced_without_aborting(self):
        captured: dict = {}
        service = self._build_service_with_batch(captured)

        def run_batch():
            callback = service.new_batch_http_request.call_args.kwargs["callback"]
            callback("id-1", None, HttpError(resp=Mock(status=404, reason="Not Found"), content=b"gone"))
            callback(
                "id-2",
                {
                    "threadId": "t2",
                    "payload": {
                        "headers": [{"name": "Subject", "value": "Two"}]
                    },
                },
                None,
            )

        captured["batch"].execute.side_effect = run_batch

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            rows = gmail_client.get_messages_metadata(["id-1", "id-2"])

        by_id = {r["id"]: r for r in rows}
        assert "error" in by_id["id-1"]
        assert by_id["id-2"]["subject"] == "Two"


class TestMessageParsing:
    def test_decode_body_handles_missing_base64_padding(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _urlsafe_b64("Hello Cusco", strip_padding=True)},
        }

        assert gmail_client._decode_body(payload) == "Hello Cusco"

    def test_decode_body_prefers_plain_text_part(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _urlsafe_b64("<p>Hello</p>")},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": _urlsafe_b64("Hello from plain text")},
                },
            ],
        }

        assert gmail_client._decode_body(payload) == "Hello from plain text"

    def test_extract_message_parts_defaults_missing_headers(self):
        message = {"threadId": "thread-1", "payload": {"headers": [], "parts": []}}

        parts = gmail_client.extract_message_parts(message)

        assert parts == {
            "subject": "(no subject)",
            "from_": "",
            "to": "",
            "body": "",
            "thread_id": "thread-1",
            "message_id_header": "",
            "references": "",
        }

    def test_extract_message_parts_reads_headers_and_body(self):
        message = {
            "threadId": "thread-9",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Tour inquiry"},
                    {"name": "From", "value": "guest@example.com"},
                    {"name": "To", "value": "info@wayonagio.com"},
                    {"name": "Message-Id", "value": "<msg-9@example.com>"},
                    {"name": "References", "value": "<old@example.com>"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _urlsafe_b64("Need pricing for Cusco tours")},
            },
        }

        parts = gmail_client.extract_message_parts(message)

        assert parts["subject"] == "Tour inquiry"
        assert parts["from_"] == "guest@example.com"
        assert parts["to"] == "info@wayonagio.com"
        assert parts["body"] == "Need pricing for Cusco tours"
        assert parts["thread_id"] == "thread-9"
        assert parts["message_id_header"] == "<msg-9@example.com>"
        assert parts["references"] == "<old@example.com>"
