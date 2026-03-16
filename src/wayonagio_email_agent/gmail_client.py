"""Gmail API wrapper.

Handles OAuth2 credential loading/refresh and provides:
  - list_messages(q, max_results)
  - get_message(message_id)
  - thread_has_draft(thread_id)  -- dedup safety check
  - draft_reply(...)             -- ONLY drafts.create, never send
"""

from __future__ import annotations

import base64
import logging
import os
from email.mime.text import MIMEText
from typing import Any

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _credentials_path() -> str:
    return os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")


def _token_path() -> str:
    return os.environ.get("GMAIL_TOKEN_PATH", "token.json")


def load_credentials() -> Credentials:
    """Load and refresh OAuth2 credentials from token.json.

    Raises SystemExit with an actionable message if re-authentication is needed.
    """
    token_path = _token_path()
    creds: Credentials | None = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
            logger.debug("OAuth token refreshed successfully.")
            return creds
        except RefreshError as exc:
            logger.error(
                "OAuth token refresh failed (%s). "
                "Re-run authentication: uv run python -m wayonagio_email_agent.cli auth",
                exc,
            )
            raise SystemExit(1) from exc

    logger.error(
        "No valid OAuth token found at '%s'. "
        "Run: uv run python -m wayonagio_email_agent.cli auth",
        token_path,
    )
    raise SystemExit(1)


def run_auth_flow() -> Credentials:
    """Interactive OAuth2 flow for first-time setup. Writes token.json."""
    credentials_path = _credentials_path()
    if not os.path.exists(credentials_path):
        logger.error(
            "OAuth client secrets not found at '%s'. "
            "Download credentials.json from the Google Cloud Console.",
            credentials_path,
        )
        raise SystemExit(1)

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds)
    logger.info("Authentication successful. Token saved to '%s'.", _token_path())
    return creds


def _save_credentials(creds: Credentials) -> None:
    with open(_token_path(), "w") as fh:
        fh.write(creds.to_json())


def _build_service() -> Any:
    creds = load_credentials()
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_messages(q: str = "is:unread", max_results: int = 20) -> list[dict]:
    """Return a list of message metadata dicts matching query *q*."""
    try:
        service = _build_service()
        result = (
            service.users()
            .messages()
            .list(userId="me", q=q, maxResults=max_results)
            .execute()
        )
        return result.get("messages", [])
    except HttpError as exc:
        logger.error("Gmail API error listing messages (q=%r): %s", q, exc)
        raise


def get_message(message_id: str) -> dict:
    """Return full message payload for *message_id*."""
    try:
        service = _build_service()
        return (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError as exc:
        logger.error("Gmail API error fetching message %s: %s", message_id, exc)
        raise


def thread_has_draft(thread_id: str) -> bool:
    """Return True if there is already a draft in *thread_id*.

    Used as a secondary dedup safety check before calling draft_reply().
    """
    try:
        service = _build_service()
        result = service.users().drafts().list(userId="me").execute()
        drafts = result.get("drafts", [])
        for draft in drafts:
            draft_detail = (
                service.users()
                .drafts()
                .get(userId="me", id=draft["id"], format="metadata")
                .execute()
            )
            if draft_detail.get("message", {}).get("threadId") == thread_id:
                return True
        return False
    except HttpError as exc:
        logger.warning(
            "Gmail API error checking drafts for thread %s: %s. Assuming no draft.",
            thread_id,
            exc,
        )
        return False


def draft_reply(
    *,
    thread_id: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str,
    references: str,
) -> dict:
    """Create a draft reply in *thread_id*. Never sends.

    Returns the created draft resource dict.
    """
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    mime["In-Reply-To"] = in_reply_to
    mime["References"] = references

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    draft_body = {"message": {"threadId": thread_id, "raw": raw}}

    try:
        service = _build_service()
        draft = (
            service.users().drafts().create(userId="me", body=draft_body).execute()
        )
        logger.info("Draft created (id=%s, thread=%s).", draft.get("id"), thread_id)
        return draft
    except HttpError as exc:
        logger.error(
            "Gmail API error creating draft in thread %s: %s", thread_id, exc
        )
        raise


# ---------------------------------------------------------------------------
# Message parsing helpers
# ---------------------------------------------------------------------------

def _decode_body(payload: dict) -> str:
    """Extract plain-text body from a message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _decode_body(part)
        if text:
            return text
    return ""


def extract_message_parts(message: dict) -> dict:
    """Return a dict with keys: subject, from_, to, body, thread_id,
    message_id_header (for In-Reply-To / References).
    """
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    return {
        "subject": headers.get("subject", "(no subject)"),
        "from_": headers.get("from", ""),
        "to": headers.get("to", ""),
        "body": _decode_body(payload),
        "thread_id": message.get("threadId", ""),
        "message_id_header": headers.get("message-id", ""),
        "references": headers.get("references", ""),
    }
