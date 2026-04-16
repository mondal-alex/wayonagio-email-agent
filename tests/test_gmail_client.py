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


class TestThreadHasDraft:
    def test_returns_true_when_thread_draft_exists(self):
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value
        drafts_resource.list.return_value.execute.return_value = {
            "drafts": [{"id": "draft-a"}, {"id": "draft-b"}]
        }
        drafts_resource.get.side_effect = [
            Mock(execute=Mock(return_value={"message": {"threadId": "other-thread"}})),
            Mock(execute=Mock(return_value={"message": {"threadId": "thread-42"}})),
        ]

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            assert gmail_client.thread_has_draft("thread-42") is True

    def test_returns_false_when_thread_draft_missing(self):
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value
        drafts_resource.list.return_value.execute.return_value = {"drafts": [{"id": "draft-a"}]}
        drafts_resource.get.return_value.execute.return_value = {
            "message": {"threadId": "different-thread"}
        }

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            assert gmail_client.thread_has_draft("thread-42") is False

    def test_raises_when_gmail_api_check_fails(self):
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value
        drafts_resource.list.return_value.execute.side_effect = HttpError(
            resp=Mock(status=500, reason="boom"),
            content=b"failed",
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            with pytest.raises(HttpError):
                gmail_client.thread_has_draft("thread-42")

    def test_paginates_draft_list_until_match(self):
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value

        first_page = {"drafts": [{"id": "draft-a"}], "nextPageToken": "next-1"}
        second_page = {"drafts": [{"id": "draft-b"}]}
        drafts_resource.list.return_value.execute.side_effect = [first_page, second_page]

        drafts_resource.get.side_effect = [
            Mock(execute=Mock(return_value={"message": {"threadId": "other-thread"}})),
            Mock(execute=Mock(return_value={"message": {"threadId": "thread-42"}})),
        ]

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            assert gmail_client.thread_has_draft("thread-42") is True

        assert drafts_resource.list.call_count == 2


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
