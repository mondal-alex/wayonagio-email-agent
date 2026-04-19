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
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from wayonagio_email_agent import agent
from wayonagio_email_agent.exemplars import loader as exemplar_loader
from wayonagio_email_agent.kb.config import KBConfigError
from wayonagio_email_agent.kb.retrieve import KBUnavailableError
from wayonagio_email_agent.llm.client import EmptyReplyError

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
# Lifespan: warm up the exemplar cache before accepting traffic
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Pre-populate the exemplar cache during container startup.

    Without this, the first user request after a Cloud Run cold start
    would pay the Drive read latency itself (~1s parallelized for ~30
    Docs). Running the load here moves that cost into the container boot
    window, which Cloud Run hides behind its startup probe (default 240s
    tolerance — well above our worst case).

    ``exemplar_loader.get_all_exemplars`` is contracted to never raise, so
    a Drive outage at startup cannot block the app from coming up. In
    that scenario the loader caches ``[]`` for the lifetime of the
    process and drafts simply omit the EXAMPLE RESPONSES block — exactly
    the same graceful degradation as the runtime path.
    """
    exemplar_loader.get_all_exemplars()
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Wayonagio Email Agent", lifespan=_lifespan)
# Middleware ordering matters: ``add_middleware`` is LIFO, so the LAST one
# added runs FIRST (outermost). _SecurityHeadersMiddleware must be the
# outermost so it still attaches the headers to short-circuit responses
# (e.g. a 413 from _BodySizeLimitMiddleware) — otherwise an attacker who
# triggers any middleware-level rejection bypasses HSTS / X-Frame-Options.
app.add_middleware(_BodySizeLimitMiddleware)
app.add_middleware(_SecurityHeadersMiddleware)

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
def draft_reply(body: DraftReplyRequest) -> DraftReplyResponse:
    """Create a draft reply for the given Gmail message ID.

    Defined with ``def`` (not ``async def``) on purpose: ``manual_draft_flow``
    is a synchronous chain of blocking I/O — Gmail HTTPS round-trips, an LLM
    completion call (multi-second), SQLite reads against the KB index, and
    optionally a GCS download. FastAPI runs sync handlers in its threadpool,
    so concurrent requests don't serialize on a single event loop. Marking
    this ``async`` would block every other request (including ``/healthz``)
    for the entire duration of one draft.
    """
    logger.info("POST /draft-reply message_id=%s language=%s", body.message_id, body.language)
    try:
        draft = agent.manual_draft_flow(body.message_id, forced_language=body.language)
    except SystemExit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail authentication failed. Run `cli auth` on the server.",
        )
    except (KBUnavailableError, KBConfigError) as exc:
        # The KB is a hard dependency: every draft must be grounded in agency
        # content. When the index is missing, corrupt, or misconfigured we
        # surface a 503 with an actionable message so the Add-on user knows
        # to escalate to the operator rather than refresh blindly.
        logger.error(
            "KB unavailable for %s: %s", body.message_id, exc, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Knowledge base unavailable. Ask the operator to run kb-ingest.",
        )
    except EmptyReplyError as exc:
        # The LLM returned nothing usable (rate limit, content filter, broken
        # provider). 502 reflects that the upstream provider, not us, failed.
        # The Add-on shows the detail so the user can simply retry.
        logger.error(
            "LLM returned empty reply for %s: %s", body.message_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM returned an empty reply. Please retry.",
        )
    except Exception as exc:
        logger.error("Draft flow failed for %s: %s", body.message_id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Draft creation failed. Check server logs.",
        )

    return DraftReplyResponse(draft_id=draft.get("id", ""))
