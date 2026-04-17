"""FastAPI application.

Endpoints:
  GET  /healthz       — unauthenticated liveness probe for Cloud Run / monitors
  POST /draft-reply   { "message_id": "...", "language": "it|es|en" (optional) }
      Requires Authorization: Bearer <AUTH_BEARER_TOKEN>
      Calls the manual draft flow and returns the created draft ID.

Run with:
  uv run uvicorn wayonagio_email_agent.api:app --host 0.0.0.0
"""

from __future__ import annotations

import hmac
import logging
import os
import traceback
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from wayonagio_email_agent import agent

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits and hardening constants
# ---------------------------------------------------------------------------
# Generous cap — the real payload is tens of bytes (a Gmail message_id + a
# language code). 16 KB leaves plenty of margin while cheaply shutting down
# abusive clients that try to stuff the body.
_MAX_BODY_BYTES = 16 * 1024

_SECURITY_HEADERS = {
    # Cloud Run already serves HTTPS; HSTS tells browsers (and any intermediary)
    # never to fall back to plain HTTP for this origin. One year, include subs.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    # Defense in depth if any error body gets treated as HTML somewhere.
    "X-Content-Type-Options": "nosniff",
    # The API never renders in a browser frame; refuse to be framed.
    "X-Frame-Options": "DENY",
    # Don't leak the request URL when the (non-existent) frontend follows links.
    "Referrer-Policy": "no-referrer",
}


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class _BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared Content-Length exceeds _MAX_BODY_BYTES.

    Catches the cheap DoS case where an attacker tries to flood the server
    with a huge JSON body. Real payloads are tiny.
    """

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={"detail": "Request body too large."},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header."},
                )
        return await call_next(request)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach a small set of defensive response headers to every response."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Wayonagio Email Agent")
app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_BodySizeLimitMiddleware)

_bearer_scheme = HTTPBearer()


def _verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> None:
    """Validate the Bearer token in constant time.

    Uses ``hmac.compare_digest`` so that an attacker cannot learn the expected
    token byte-by-byte through timing measurements.
    """
    expected = os.environ.get("AUTH_BEARER_TOKEN", "")
    if not expected:
        logger.error("AUTH_BEARER_TOKEN is not set. Rejecting all requests.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication is not configured.",
        )
    if not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
        )


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception on %s %s:\n%s",
        request.method,
        request.url,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error. Check server logs."},
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class DraftReplyRequest(BaseModel):
    message_id: str
    language: Literal["it", "es", "en"] | None = None


class DraftReplyResponse(BaseModel):
    draft_id: str
    message: str = "Draft created successfully."


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Unauthenticated liveness probe.

    Intentionally does *not* call Gmail or the LLM provider. Its only job is to
    prove the process is up, so platform health checks (Cloud Run, uptime
    monitors) don't depend on external services being reachable.
    """
    return HealthResponse()


@app.post(
    "/draft-reply",
    response_model=DraftReplyResponse,
    dependencies=[Depends(_verify_token)],
)
async def draft_reply(body: DraftReplyRequest) -> DraftReplyResponse:
    """Create a draft reply for the given Gmail message ID."""
    logger.info("POST /draft-reply message_id=%s language=%s", body.message_id, body.language)
    try:
        draft = agent.manual_draft_flow(body.message_id, forced_language=body.language)
    except SystemExit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail authentication failed. Run `cli auth` on the server.",
        )
    except Exception as exc:
        logger.error("Draft flow failed for %s: %s", body.message_id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Draft creation failed. Check server logs.",
        )

    return DraftReplyResponse(draft_id=draft.get("id", ""))
