"""Unit tests for api.py.

Uses FastAPI TestClient (via httpx). Agent calls are mocked.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Set env vars before importing the app so logging.basicConfig uses them
os.environ.setdefault("AUTH_BEARER_TOKEN", "test-token")
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
