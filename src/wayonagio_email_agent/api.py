"""FastAPI application.

Endpoints:
  POST /draft-reply   { "message_id": "..." }
      Requires Authorization: Bearer <AUTH_BEARER_TOKEN>
      Calls the manual draft flow and returns the created draft ID.

Run with:
  uv run uvicorn wayonagio_email_agent.api:app --host 0.0.0.0
"""

from __future__ import annotations

import logging
import os
import traceback

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

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
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Wayonagio Email Agent")

_bearer_scheme = HTTPBearer()


def _verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> None:
    expected = os.environ.get("AUTH_BEARER_TOKEN", "")
    if not expected:
        logger.error("AUTH_BEARER_TOKEN is not set. Rejecting all requests.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication is not configured.",
        )
    if credentials.credentials != expected:
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


class DraftReplyResponse(BaseModel):
    draft_id: str
    message: str = "Draft created successfully."


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.post(
    "/draft-reply",
    response_model=DraftReplyResponse,
    dependencies=[Depends(_verify_token)],
)
async def draft_reply(body: DraftReplyRequest) -> DraftReplyResponse:
    """Create a draft reply for the given Gmail message ID."""
    logger.info("POST /draft-reply message_id=%s", body.message_id)
    try:
        draft = agent.manual_draft_flow(body.message_id)
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
