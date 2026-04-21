"""Unit tests for gmail_client.py."""

from __future__ import annotations

import base64
from email import message_from_bytes
from unittest.mock import Mock, patch

import pytest
from google.auth.exceptions import RefreshError
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

    def test_does_not_double_prefix_when_subject_has_leading_whitespace(self):
        """Line-folded Subject headers occasionally reach us with leading
        whitespace. Without the strip/normalize step, ``startswith("re:")``
        misses and we'd emit an ugly ``Re:  Re: Existing`` subject.
        """
        service = Mock()
        drafts_resource = service.users.return_value.drafts.return_value
        drafts_resource.create.return_value.execute.return_value = {"id": "draft-ws"}

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            gmail_client.draft_reply(
                thread_id="thread-ws",
                to="guest@example.com",
                subject="  Re: Existing thread",
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


class TestThreadTranscript:
    def _thread_message(
        self,
        *,
        mid: str,
        internal_date: str | None,
        sender: str,
        subject: str,
        body: str,
        label_ids: list[str] | None = None,
    ) -> dict:
        msg: dict = {
            "id": mid,
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": subject},
                ],
                "mimeType": "text/plain",
                "body": {"data": _urlsafe_b64(body)},
            },
            "labelIds": label_ids or ["INBOX"],
        }
        if internal_date is not None:
            msg["internalDate"] = internal_date
        return msg

    def test_includes_messages_up_to_anchor_excluding_drafts(self):
        thread = {
            "messages": [
                # Out-of-order on purpose: should be sorted by internalDate.
                self._thread_message(
                    mid="m3",
                    internal_date="3000",
                    sender="guest@example.com",
                    subject="Third",
                    body="Third body",
                ),
                self._thread_message(
                    mid="m1",
                    internal_date="1000",
                    sender="guest@example.com",
                    subject="First",
                    body="First body",
                ),
                # Draft must be excluded from transcript.
                self._thread_message(
                    mid="md",
                    internal_date="1500",
                    sender="staff@example.com",
                    subject="Draft",
                    body="Draft text",
                    label_ids=["DRAFT"],
                ),
                self._thread_message(
                    mid="m2",
                    internal_date="2000",
                    sender="staff@example.com",
                    subject="Second",
                    body="Second body",
                ),
            ]
        }
        service = Mock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
            thread
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            transcript = gmail_client.build_thread_transcript(
                thread_id="t-1",
                anchor_message_id="m2",
                max_chars=10_000,
            )

        assert "First body" in transcript
        assert "Second body" in transcript
        assert "Third body" not in transcript, "messages after anchor must be excluded"
        assert "Draft text" not in transcript, "DRAFT-labeled messages must be excluded"

        first_idx = transcript.index("First body")
        second_idx = transcript.index("Second body")
        assert first_idx < second_idx

    def test_truncates_oldest_messages_first_with_banner(self):
        thread = {
            "messages": [
                self._thread_message(
                    mid="m1",
                    internal_date="1000",
                    sender="a@example.com",
                    subject="S1",
                    body="A" * 300,
                ),
                self._thread_message(
                    mid="m2",
                    internal_date="2000",
                    sender="b@example.com",
                    subject="S2",
                    body="B" * 300,
                ),
                self._thread_message(
                    mid="m3",
                    internal_date="3000",
                    sender="c@example.com",
                    subject="S3",
                    body="C" * 300,
                ),
            ]
        }
        service = Mock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
            thread
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            transcript = gmail_client.build_thread_transcript(
                thread_id="t-1",
                anchor_message_id="m3",
                max_chars=500,
            )

        assert transcript.startswith("[Earlier thread messages omitted")
        assert "A" * 100 not in transcript
        assert ("B" * 100 in transcript) or ("C" * 100 in transcript)

    def test_logs_fetch_and_truncation_stats(self, caplog):
        thread = {
            "messages": [
                self._thread_message(
                    mid="m1",
                    internal_date="1000",
                    sender="a@example.com",
                    subject="S1",
                    body="A" * 300,
                ),
                self._thread_message(
                    mid="m2",
                    internal_date="2000",
                    sender="b@example.com",
                    subject="S2",
                    body="B" * 300,
                ),
            ]
        }
        service = Mock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
            thread
        )

        caplog.set_level("INFO", logger="wayonagio_email_agent.gmail_client")
        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            gmail_client.build_thread_transcript(
                thread_id="t-logs",
                anchor_message_id="m2",
                max_chars=300,
            )

        messages = [rec.message for rec in caplog.records]
        assert any("fetched 2 message(s)" in m and "non-draft message(s)" in m for m in messages)
        assert any("transcript truncated" in m and "dropped" in m for m in messages)

    def test_keeps_messages_with_invalid_internaldate_using_api_order_fallback(self):
        thread = {
            "messages": [
                self._thread_message(
                    mid="m1",
                    internal_date=None,
                    sender="a@example.com",
                    subject="NoDate",
                    body="NoDate body",
                ),
                self._thread_message(
                    mid="m2",
                    internal_date="2000",
                    sender="b@example.com",
                    subject="HasDate",
                    body="HasDate body",
                ),
                self._thread_message(
                    mid="m3",
                    internal_date="not-a-number",
                    sender="c@example.com",
                    subject="BadDate",
                    body="BadDate body",
                ),
            ]
        }
        service = Mock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
            thread
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            transcript = gmail_client.build_thread_transcript(
                thread_id="t-1",
                anchor_message_id="m3",
                max_chars=10_000,
            )

        assert "NoDate body" in transcript
        assert "HasDate body" in transcript
        assert "BadDate body" in transcript
        assert transcript.index("NoDate body") < transcript.index("HasDate body")
        assert transcript.index("HasDate body") < transcript.index("BadDate body")

    def test_omission_banner_survives_tight_budget(self):
        thread = {
            "messages": [
                self._thread_message(
                    mid="m1",
                    internal_date="1000",
                    sender="a@example.com",
                    subject="S1",
                    body="X" * 600,
                ),
                self._thread_message(
                    mid="m2",
                    internal_date="2000",
                    sender="b@example.com",
                    subject="S2",
                    body="Y" * 600,
                ),
            ]
        }
        service = Mock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
            thread
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            transcript = gmail_client.build_thread_transcript(
                thread_id="t-1",
                anchor_message_id="m2",
                max_chars=220,
            )

        assert transcript.startswith("[Earlier thread messages omitted")

    def test_raises_when_anchor_missing_after_filtering(self):
        thread = {
            "messages": [
                self._thread_message(
                    mid="m1",
                    internal_date="1000",
                    sender="a@example.com",
                    subject="First",
                    body="First body",
                )
            ]
        }
        service = Mock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = (
            thread
        )

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            with pytest.raises(ValueError, match="Anchor message not found"):
                gmail_client.build_thread_transcript(
                    thread_id="t-1",
                    anchor_message_id="missing",
                    max_chars=10_000,
                )


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

    def test_chunks_large_batches_below_per_user_concurrency_cap(self, monkeypatch):
        """Gmail caps concurrent requests per user around 10; bundling 50
        messages.get calls into one batch reliably 429s the overflow with
        "Too many concurrent requests for user." We must split the batch
        into chunks of <= ``_BATCH_CHUNK_SIZE`` so each round-trip stays
        inside the cap.
        """
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)

        executed_chunk_sizes: list[int] = []

        def make_batch():
            batch = Mock()
            chunk_added: list[str] = []

            def fake_add(request, request_id):
                chunk_added.append(request_id)

            def fake_execute():
                executed_chunk_sizes.append(len(chunk_added))
                callback = service.new_batch_http_request.call_args.kwargs["callback"]
                for rid in chunk_added:
                    callback(
                        rid,
                        {
                            "threadId": f"t-{rid}",
                            "payload": {"headers": [{"name": "Subject", "value": rid}]},
                        },
                        None,
                    )

            batch.add.side_effect = fake_add
            batch.execute.side_effect = fake_execute
            return batch

        service = Mock()
        service.new_batch_http_request = Mock(side_effect=lambda callback: make_batch())

        ids = [f"id-{i}" for i in range(25)]
        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            rows = gmail_client.get_messages_metadata(ids)

        # 25 ids / 10 per chunk = 3 chunks (10, 10, 5).
        assert executed_chunk_sizes == [10, 10, 5]
        assert all(size <= gmail_client._BATCH_CHUNK_SIZE for size in executed_chunk_sizes)
        assert len(rows) == 25
        assert {r["id"] for r in rows} == set(ids)

    def test_retries_per_message_429s_with_backoff(self, monkeypatch):
        """A 429 on a specific id inside the batch must be retried — that's
        the recoverable case (transient concurrency spike). 404s and other
        statuses must NOT be retried, since waiting won't help.
        """
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)

        attempt_history: dict[str, int] = {}

        def make_batch():
            batch = Mock()
            chunk_added: list[str] = []

            def fake_add(request, request_id):
                chunk_added.append(request_id)

            def fake_execute():
                callback = service.new_batch_http_request.call_args.kwargs["callback"]
                for rid in chunk_added:
                    attempts = attempt_history.get(rid, 0) + 1
                    attempt_history[rid] = attempts
                    if rid == "id-flaky" and attempts == 1:
                        # Transient 429 on first attempt, succeeds on retry.
                        callback(
                            rid,
                            None,
                            HttpError(
                                resp=Mock(
                                    status=429,
                                    reason="Too Many Requests",
                                ),
                                content=b"slow down",
                            ),
                        )
                    elif rid == "id-gone":
                        callback(
                            rid,
                            None,
                            HttpError(
                                resp=Mock(status=404, reason="Not Found"),
                                content=b"gone",
                            ),
                        )
                    else:
                        callback(
                            rid,
                            {
                                "threadId": f"t-{rid}",
                                "payload": {
                                    "headers": [{"name": "Subject", "value": rid}]
                                },
                            },
                            None,
                        )

            batch.add.side_effect = fake_add
            batch.execute.side_effect = fake_execute
            return batch

        service = Mock()
        service.new_batch_http_request = Mock(side_effect=lambda callback: make_batch())

        with patch("wayonagio_email_agent.gmail_client._build_service", return_value=service):
            rows = gmail_client.get_messages_metadata(
                ["id-1", "id-flaky", "id-gone"]
            )

        by_id = {r["id"]: r for r in rows}
        # 429 was retried and recovered.
        assert by_id["id-flaky"]["subject"] == "id-flaky"
        assert attempt_history["id-flaky"] == 2
        # 404 was NOT retried — that would just slow the failure down.
        assert "error" in by_id["id-gone"]
        assert attempt_history["id-gone"] == 1
        # Healthy id was fetched once.
        assert by_id["id-1"]["subject"] == "id-1"
        assert attempt_history["id-1"] == 1


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
            "received_at": "",
        }

    def test_extract_message_parts_reads_headers_and_body(self):
        message = {
            "threadId": "thread-9",
            # Epoch ms for 2021-01-01 00:00:00 UTC — disambiguates list rows.
            "internalDate": "1609459200000",
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
        assert parts["received_at"] == "2021-01-01 00:00 UTC"


class TestLoadCredentials:
    """OAuth credential loading anchors the API's 503 contract.

    `manual_draft_flow` -> `_build_service` -> `load_credentials`. If
    `load_credentials` ever stops raising `SystemExit` on auth failures, the
    api.py 503 mapping silently turns into a 500. These tests freeze the
    contract.
    """

    def test_returns_valid_credentials_directly(self, monkeypatch, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))

        creds = Mock()
        creds.valid = True

        with patch(
            "wayonagio_email_agent.gmail_client.Credentials.from_authorized_user_file",
            return_value=creds,
        ):
            result = gmail_client.load_credentials()

        assert result is creds

    def test_refreshes_expired_token_and_persists(self, monkeypatch, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))

        creds = Mock()
        creds.valid = False
        creds.expired = True
        creds.refresh_token = "refresh-abc"
        creds.to_json.return_value = '{"refreshed": true}'

        def _refresh(_request):
            creds.valid = True

        creds.refresh.side_effect = _refresh

        with patch(
            "wayonagio_email_agent.gmail_client.Credentials.from_authorized_user_file",
            return_value=creds,
        ):
            result = gmail_client.load_credentials()

        assert result is creds
        creds.refresh.assert_called_once()
        # Token must be re-persisted after refresh so the new access token
        # survives the next process restart.
        assert token_path.read_text() == '{"refreshed": true}'

    def test_systemexit_when_refresh_fails(self, monkeypatch, tmp_path):
        """Locks the API contract: a refresh failure must raise SystemExit
        so api.py can map it to HTTP 503."""
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))

        creds = Mock()
        creds.valid = False
        creds.expired = True
        creds.refresh_token = "refresh-abc"
        creds.refresh.side_effect = RefreshError("token revoked")

        with patch(
            "wayonagio_email_agent.gmail_client.Credentials.from_authorized_user_file",
            return_value=creds,
        ):
            with pytest.raises(SystemExit):
                gmail_client.load_credentials()


class TestBuildService:
    def test_disables_discovery_file_cache_noise(self):
        creds = Mock()
        with (
            patch("wayonagio_email_agent.gmail_client.load_credentials", return_value=creds),
            patch("wayonagio_email_agent.gmail_client.build") as mock_build,
        ):
            gmail_client._build_service()

        mock_build.assert_called_once_with(
            "gmail", "v1", credentials=creds, cache_discovery=False
        )

    def test_systemexit_when_token_missing(self, monkeypatch, tmp_path):
        token_path = tmp_path / "missing.json"
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))

        with pytest.raises(SystemExit):
            gmail_client.load_credentials()

    def test_systemexit_when_token_present_but_unrefreshable(
        self, monkeypatch, tmp_path
    ):
        """Token file exists but creds are invalid AND not refreshable
        (no refresh_token). Must still hard-exit."""
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))

        creds = Mock()
        creds.valid = False
        creds.expired = False
        creds.refresh_token = None

        with patch(
            "wayonagio_email_agent.gmail_client.Credentials.from_authorized_user_file",
            return_value=creds,
        ):
            with pytest.raises(SystemExit):
                gmail_client.load_credentials()


class TestListMessages:
    def test_returns_message_list(self):
        service = Mock()
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "m1"}, {"id": "m2"}],
        }

        with patch(
            "wayonagio_email_agent.gmail_client._build_service",
            return_value=service,
        ):
            result = gmail_client.list_messages(q="is:unread", max_results=20)

        assert result == [{"id": "m1"}, {"id": "m2"}]
        service.users.return_value.messages.return_value.list.assert_called_once_with(
            userId="me", q="is:unread", maxResults=20
        )

    def test_returns_empty_list_when_no_messages_key(self):
        service = Mock()
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {}

        with patch(
            "wayonagio_email_agent.gmail_client._build_service",
            return_value=service,
        ):
            result = gmail_client.list_messages()

        assert result == []

    def test_propagates_http_error(self):
        service = Mock()
        service.users.return_value.messages.return_value.list.return_value.execute.side_effect = HttpError(
            resp=Mock(status=500, reason="boom"),
            content=b"failed",
        )

        with patch(
            "wayonagio_email_agent.gmail_client._build_service",
            return_value=service,
        ):
            with pytest.raises(HttpError):
                gmail_client.list_messages()


class TestGetMessage:
    def test_returns_full_message_payload(self):
        service = Mock()
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": "m1",
            "payload": {},
        }

        with patch(
            "wayonagio_email_agent.gmail_client._build_service",
            return_value=service,
        ):
            result = gmail_client.get_message("m1")

        assert result["id"] == "m1"
        service.users.return_value.messages.return_value.get.assert_called_once_with(
            userId="me", id="m1", format="full"
        )

    def test_propagates_http_error(self):
        service = Mock()
        service.users.return_value.messages.return_value.get.return_value.execute.side_effect = HttpError(
            resp=Mock(status=404, reason="Not Found"),
            content=b"missing",
        )

        with patch(
            "wayonagio_email_agent.gmail_client._build_service",
            return_value=service,
        ):
            with pytest.raises(HttpError):
                gmail_client.get_message("m1")
