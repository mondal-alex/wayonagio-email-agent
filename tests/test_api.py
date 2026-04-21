"""Unit tests for api.py.

Uses FastAPI TestClient (via httpx). Agent calls are mocked.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

# Set env vars before importing the app so logging.basicConfig uses them.
# Use direct assignment (not setdefault) because sibling modules may have
# already loaded `.env` into os.environ via python-dotenv, which would shadow
# the expected test token.
os.environ["AUTH_BEARER_TOKEN"] = "test-token"
os.environ.setdefault("LOG_LEVEL", "WARNING")

from wayonagio_email_agent.api import app  # noqa: E402
from wayonagio_email_agent.kb.config import KBConfigError  # noqa: E402
from wayonagio_email_agent.kb.retrieve import KBUnavailableError  # noqa: E402
from wayonagio_email_agent.llm.client import EmptyReplyError  # noqa: E402

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

    def test_kb_unavailable_returns_503_with_actionable_message(self):
        """KB failures are the most common runtime error after the KB became
        required. The Add-on user MUST get a message that points to the fix
        (kb-ingest) instead of the generic "check server logs"."""
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            side_effect=KBUnavailableError("KB index artifact could not be downloaded."),
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-kb"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 503
        detail = resp.json()["detail"].lower()
        assert "knowledge base" in detail
        assert "kb-ingest" in detail

    def test_kb_config_error_returns_503(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            side_effect=KBConfigError("KB_RAG_FOLDER_IDS is required."),
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-kbc"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 503
        assert "knowledge base" in resp.json()["detail"].lower()

    def test_end_to_end_kb_missing_returns_503(self, tmp_path, monkeypatch):
        """End-to-end (no manual_draft_flow stub): exercises the real
        ``generate_reply`` → ``kb_retrieve.retrieve`` path. With
        ``KB_RAG_FOLDER_IDS`` set but no artifact on disk, retrieval must
        raise ``KBUnavailableError`` and the API must map it to 503.

        This freezes the contract that the api.py error mapping survives
        even when callers don't mock the agent layer."""
        from wayonagio_email_agent import gmail_client
        from wayonagio_email_agent.kb import retrieve as kb_retrieve
        from wayonagio_email_agent.llm import client as llm_module

        # Empty cache dir → artifact.download_artifact returns None → raises.
        monkeypatch.setenv("KB_RAG_FOLDER_IDS", "fake-folder-id")
        monkeypatch.setenv("KB_LOCAL_DIR", str(tmp_path / "no_kb_here"))
        monkeypatch.delenv("KB_GCS_URI", raising=False)
        kb_retrieve.reset_cache()

        fake_message = {"id": "msg-end-to-end"}
        fake_parts = {
            "subject": "Tour pricing",
            "from_": "guest@example.com",
            "to": "info@wayonagio.com",
            "body": "Hello, what does the Machu Picchu tour cost?",
            "thread_id": "thread-1",
            "message_id_header": "<msg@example.com>",
            "references": "",
        }

        with (
            patch.object(gmail_client, "get_message", return_value=fake_message),
            patch.object(
                gmail_client, "extract_message_parts", return_value=fake_parts
            ),
            patch.object(
                gmail_client,
                "build_thread_transcript",
                return_value=fake_parts["body"],
            ),
            patch.object(llm_module, "detect_language", return_value="en"),
            # If 503 mapping is broken, generate_reply will be called and we'd
            # hit a real LLM; assert it's never reached.
            patch.object(
                llm_module, "_chat", side_effect=AssertionError("LLM was called")
            ),
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-end-to-end"},
                headers=_GOOD_HEADERS,
            )

        assert resp.status_code == 503, resp.text
        assert "kb-ingest" in resp.json()["detail"].lower()

    def test_empty_reply_returns_502(self):
        with patch(
            "wayonagio_email_agent.api.agent.manual_draft_flow",
            side_effect=EmptyReplyError("LLM returned an empty reply."),
        ):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "msg-empty"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 502
        assert "empty reply" in resp.json()["detail"].lower()

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


class TestGlobalExceptionHandler:
    """Anchors the contract that ANY uncaught exception is converted to a
    generic 500 (no traceback / exception text leaked) and that the security
    headers are still attached on the error path. Without this test, a
    future bug in a route could leak a stack trace into the response body
    or skip the HSTS/X-Frame-Options headers on errors.

    We exercise both halves separately because Starlette's BaseHTTPMiddleware
    bypasses FastAPI's @app.exception_handler stack on truly-unhandled
    exceptions in route handlers, so a single end-to-end test is unreliable.
    """

    def test_handler_returns_generic_500_without_leaking_exception(self):
        """Direct unit test of the handler: it must produce a generic body
        and never echo the exception type or message into the response."""
        import asyncio

        from wayonagio_email_agent.api import _unhandled_exception_handler

        fake_request = MagicMock()
        fake_request.method = "POST"
        fake_request.url = "http://test/draft-reply"
        exc = RuntimeError("secret internal failure with credentials=hunter2")

        response = asyncio.run(_unhandled_exception_handler(fake_request, exc))

        assert response.status_code == 500
        body = response.body.decode()
        assert "internal server error" in body.lower()
        # Critically: must not leak exception type, message, or anything that
        # could expose a credential or internal detail to the caller.
        assert "RuntimeError" not in body
        assert "hunter2" not in body
        assert "Traceback" not in body

    def test_500_responses_carry_security_headers(self):
        """When the auth dependency raises a 500 (server misconfiguration),
        the security middleware must still attach all four headers. This
        also covers the contract that errors generated above the auth layer
        do not bypass _SecurityHeadersMiddleware.
        """
        with patch.dict(os.environ, {"AUTH_BEARER_TOKEN": ""}, clear=False):
            resp = client.post(
                "/draft-reply",
                json={"message_id": "m"},
                headers=_GOOD_HEADERS,
            )
        assert resp.status_code == 500
        for header in (
            "Strict-Transport-Security",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
        ):
            assert header in resp.headers, f"missing {header} on 500 response"


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
        # Freezes the middleware-ordering contract: SecurityHeaders wraps
        # BodySizeLimit, so a 413 short-circuit response must STILL pass
        # through the security headers layer. If someone reorders these,
        # this test catches it before it lands in production.
        for header in (
            "Strict-Transport-Security",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
        ):
            assert header in resp.headers, f"missing {header} on 413 response"

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

class TestExemplarWarmup:
    """Anchors the lifespan contract: the exemplar cache MUST be populated
    before the app accepts traffic, and a warm-up failure MUST NOT prevent
    the app from starting.

    Module-level ``client`` does not trigger lifespan because we don't use
    it as a context manager (intentional — most tests don't need to spin
    Drive). These tests use ``with TestClient(app)`` explicitly to exercise
    the startup hook.
    """

    def _fresh_client(self) -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    def test_lifespan_populates_exemplar_cache_before_serving(self, monkeypatch):
        from wayonagio_email_agent.exemplars import loader as exemplar_loader
        from wayonagio_email_agent.exemplars.source import Exemplar

        exemplar_loader.reset()
        sample = [Exemplar(title="A", text="body", source_id="sid")]

        called = {"count": 0}

        def _fake_get_all() -> list[Exemplar]:
            called["count"] += 1
            # Mutate the cache directly so the assertion below is real.
            exemplar_loader._cache = sample
            return sample

        monkeypatch.setattr(
            "wayonagio_email_agent.api.exemplar_loader.get_all_exemplars",
            _fake_get_all,
        )

        with self._fresh_client() as warm_client:
            # By the time the context manager has yielded a usable client,
            # FastAPI's lifespan handler has already run to its yield. The
            # cache must already be populated.
            assert exemplar_loader._cache == sample
            assert called["count"] >= 1

            # And the app must accept requests as normal.
            resp = warm_client.get("/healthz")
            assert resp.status_code == 200

        exemplar_loader.reset()

    def test_warmup_failure_does_not_block_startup(self, monkeypatch):
        """The loader is contracted to never raise. If a future refactor
        reintroduces an unhandled exception here, the app would fail to
        start and Cloud Run would crash-loop the revision. This test
        anchors that the API startup remains robust to loader failures.
        """
        from wayonagio_email_agent.exemplars import loader as exemplar_loader

        exemplar_loader.reset()

        # Simulate a loader bug that raises despite the contract — the
        # warm-up wrapper must still let the app come up. We do this by
        # patching the underlying source.collect (loader catches it).
        monkeypatch.setattr(
            "wayonagio_email_agent.exemplars.loader.exemplar_config.load",
            lambda: (_ for _ in ()).throw(RuntimeError("config exploded")),
        )

        with self._fresh_client() as warm_client:
            resp = warm_client.get("/healthz")
            assert resp.status_code == 200
            # And the cache settled to [], not None — so subsequent draft
            # calls won't re-attempt the load on every request.
            assert exemplar_loader._cache == []

        exemplar_loader.reset()


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
