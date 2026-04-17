"""Unit tests for api.py.

Uses FastAPI TestClient (via httpx). Agent calls are mocked.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

# Set env vars before importing the app so logging.basicConfig uses them.
# Use direct assignment (not setdefault) because sibling modules may have
# already loaded `.env` into os.environ via python-dotenv, which would shadow
# the expected test token.
os.environ["AUTH_BEARER_TOKEN"] = "test-token"
os.environ.setdefault("LOG_LEVEL", "WARNING")

from wayonagio_email_agent.api import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

_GOOD_HEADERS = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# Bearer auth
# ---------------------------------------------------------------------------

class TestBearerAuth:
    def test_missing_auth_header_returns_4xx(self):
        resp = client.post("/draft-reply", json={"message_id": "msg-1"})
        # HTTPBearer returns 403 (no credentials) in most FastAPI versions
        assert resp.status_code in (401, 403)

    def test_wrong_token_returns_401(self):
        resp = client.post(
            "/draft-reply",
            json={"message_id": "msg-1"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_valid_token_passes_auth(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            return_value={"id": "draft-abc"},
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-1"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /draft-reply
# ---------------------------------------------------------------------------

class TestDraftReplyEndpoint:
    def test_successful_draft_returns_draft_id(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            return_value={"id": "draft-xyz"},
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-42"},
                headers=_GOOD_HEADERS,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["draft_id"] == "draft-xyz"
        assert "successfully" in data["message"].lower()

    def test_missing_message_id_returns_422(self):
        resp = client.post("/draft-reply", json={}, headers=_GOOD_HEADERS)
        assert resp.status_code == 422

    def test_agent_runtime_error_returns_500(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            side_effect=RuntimeError("Gmail quota exceeded"),
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-err"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Draft creation failed. Check server logs."

    def test_gmail_auth_failure_returns_503(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            side_effect=SystemExit(1),
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-auth"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 503
        assert "auth" in resp.json()["detail"].lower()

    def test_unconfigured_bearer_token_returns_500(self):
        original = os.environ.pop("AUTH_BEARER_TOKEN", None)
        try:
            resp = client.post(
                "/draft-reply",
                json={"message_id": "x"},
                headers={"Authorization": "Bearer anything"},
            )
            assert resp.status_code == 500
        finally:
            if original is not None:
                os.environ["AUTH_BEARER_TOKEN"] = original

    def test_forced_language_is_forwarded_to_agent(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            return_value={"id": "draft-lang"},
        ) as mock_flow:
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-1", "language": "it"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 200
        mock_flow.assert_called_once_with("msg-1", forced_language="it")

    def test_invalid_language_returns_422(self):
        resp = client.post(
            "/draft-reply",
            json={"message_id": "msg-1", "language": "fr"},
            headers=_GOOD_HEADERS,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_returns_ok_without_auth(self):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_does_not_touch_gmail_or_llm(self):
        """The liveness probe must not reach external services.

        If it did, a Gmail/LLM outage would flap the healthcheck and Cloud Run
        would restart the container uselessly.
        """
        with (
            patch("wayonagio_email_agent.api.agent.manual_draft_flow") as mock_flow,
            patch("wayonagio_email_agent.gmail_client._build_service") as mock_gmail,
        ):
            resp = client.get("/healthz")

        assert resp.status_code == 200
        mock_flow.assert_not_called()
        mock_gmail.assert_not_called()


# ---------------------------------------------------------------------------
# Constant-time auth + hardening
# ---------------------------------------------------------------------------

class TestConstantTimeAuth:
    def test_token_differing_only_in_length_is_rejected(self):
        # hmac.compare_digest handles different-length inputs without leaking,
        # but we still need to confirm a length mismatch → 401, not 500.
        resp = client.post(
            "/draft-reply",
            json={"message_id": "x"},
            headers={"Authorization": "Bearer test-token-extra-bytes"},
        )
        assert resp.status_code == 401


class TestSecurityHeaders:
    def test_hsts_and_content_type_options_on_health_response(self):
        resp = client.get("/healthz")
        assert "Strict-Transport-Security" in resp.headers
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"

    def test_headers_also_present_on_error_responses(self):
        resp = client.post(
            "/draft-reply",
            json={"message_id": "x"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        assert "Strict-Transport-Security" in resp.headers


class TestBodySizeLimit:
    def test_oversized_body_is_rejected_with_413(self):
        # Content-Length above the 16 KiB cap must be rejected before parsing.
        resp = client.post(
            "/draft-reply",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
                "Content-Length": str(1024 * 1024),
            },
            content=b'{"message_id":"' + (b"A" * (1024 * 1024)) + b'"}',
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["detail"].lower()

    def test_normal_sized_body_passes(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            return_value={"id": "ok"},
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-small"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Malformed Content-Length
# ---------------------------------------------------------------------------

class TestMalformedContentLength:
    def test_non_numeric_content_length_is_tolerated(self):
        # Most HTTP clients normalize/overwrite Content-Length before the
        # request leaves the process, so we can't reliably send a bad value
        # via TestClient. We just assert that the endpoint doesn't crash
        # when the header is passed through unchanged — either the client
        # rewrites it (→ 200) or our middleware rejects it (→ 400).
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            return_value={"id": "ok"},
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-1"},
                headers={
                    **_GOOD_HEADERS,
                    "Content-Length": "not-a-number",
                },
            )
        assert resp.status_code in (200, 400)
