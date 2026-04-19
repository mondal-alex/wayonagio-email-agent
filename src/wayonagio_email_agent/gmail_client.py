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

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Note: `.env` is loaded by the entry points (api.py, cli.py). Library modules
# intentionally don't call load_dotenv() so they stay cleanly importable in
# tests and from other apps without implicit filesystem reads.

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    # Read-only access to Google Drive is required for the knowledge base
    # (see src/wayonagio_email_agent/kb/), which the agent uses to ground every
    # reply in agency-specific content. The KB ingest Job needs this scope to
    # walk KB_RAG_FOLDER_IDS.
    "https://www.googleapis.com/auth/drive.readonly",
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


def get_messages_metadata(
    message_ids: list[str],
    headers: tuple[str, ...] = ("Subject", "From"),
) -> list[dict]:
    """Batch-fetch header metadata for *message_ids* in a single HTTP round-trip.

    This is the N+1 fix for list-style admin commands: instead of issuing one
    ``messages.get`` request per message, we bundle every ``get`` into a single
    multipart ``batch`` request that the Gmail API services in one hop.

    Returns a list of :func:`extract_message_parts`-shaped dicts in the same
    order as *message_ids*. Messages that fail to fetch are replaced with a
    stub dict containing the id and an ``error`` key, so the caller can surface
    the failure without having the whole list blow up.
    """
    if not message_ids:
        return []

    service = _build_service()
    results: dict[str, dict] = {}
    errors: dict[str, Exception] = {}

    def _callback(request_id: str, response: dict | None, exception: Exception | None) -> None:
        if exception is not None:
            errors[request_id] = exception
        else:
            results[request_id] = response or {}

    batch = service.new_batch_http_request(callback=_callback)
    for mid in message_ids:
        batch.add(
            service.users().messages().get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=list(headers),
            ),
            request_id=mid,
        )

    try:
        batch.execute()
    except HttpError as exc:
        logger.error("Gmail API error on batch metadata fetch: %s", exc)
        raise

    output: list[dict] = []
    for mid in message_ids:
        if mid in errors:
            logger.warning("Failed to fetch message %s in batch: %s", mid, errors[mid])
            output.append({"id": mid, "error": str(errors[mid])})
            continue
        parts = extract_message_parts(results.get(mid, {}))
        parts["id"] = mid
        output.append(parts)
    return output


def thread_has_draft(thread_id: str) -> bool:
    """Return True if *thread_id* already contains a draft.

    Used as a secondary dedup safety check before calling draft_reply(). Checked
    by fetching the thread and looking for any message tagged with the ``DRAFT``
    label — this is a single API call regardless of how many drafts the mailbox
    has, which keeps us well clear of the Gmail quota on busy shared inboxes.
    """
    try:
        service = _build_service()
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="metadata")
            .execute()
        )
    except HttpError as exc:
        logger.error(
            "Gmail API error checking thread %s for drafts: %s.", thread_id, exc
        )
        raise

    for message in thread.get("messages", []):
        if "DRAFT" in message.get("labelIds", []):
            return True
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
        if not data:
            return ""
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(f"{data}{padding}").decode(
            "utf-8", errors="replace"
        )

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
