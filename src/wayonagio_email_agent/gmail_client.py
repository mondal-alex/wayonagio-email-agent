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
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Gmail's batch endpoint bundles N sub-requests into one HTTP round-trip
# but the server processes them *concurrently per user*, and Gmail enforces
# a per-user concurrency cap around 10. Packing 50 messages.get calls into
# one batch reliably trips "Too many concurrent requests for user." 429s
# on the overflow. Chunking to 10-at-a-time stays inside the cap; the
# small inter-chunk pause adds a margin for whatever else the user's
# session is doing (Gmail web UI counts against the same per-user budget).
_BATCH_CHUNK_SIZE = 10
_BATCH_INTER_CHUNK_SECONDS = 0.5
_BATCH_MAX_RETRY_PASSES = 3
_BATCH_RETRY_BACKOFF_SECONDS = 2.0
_THREAD_TRANSCRIPT_OMISSION_BANNER = (
    "[Earlier thread messages omitted — showing the most recent {count} message(s).]"
)

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
    # Suppresses noisy runtime info logs from googleapiclient's legacy discovery
    # cache path ("file_cache is only supported with oauth2client<4.0.0").
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _is_rate_limit(exc: Exception) -> bool:
    """True iff *exc* is a Gmail HTTP 429 (rate-limit) error.

    HttpError.resp.status is the stable attribute across versions of
    google-api-python-client; HttpError.status_code only appeared more
    recently.
    """
    if not isinstance(exc, HttpError):
        return False
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) == 429


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


def get_thread_full(thread_id: str) -> dict:
    """Return full Gmail thread payload for *thread_id*."""
    try:
        service = _build_service()
        return (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
    except HttpError as exc:
        logger.error("Gmail API error fetching thread %s: %s", thread_id, exc)
        raise


def build_thread_transcript(
    *,
    thread_id: str,
    anchor_message_id: str,
    max_chars: int,
) -> str:
    """Build a chronological transcript through *anchor_message_id*.

    Transcript rules:
    - Uses `threads.get(format="full")` once.
    - Excludes messages labeled DRAFT.
    - Orders by internalDate ascending when available, falling back to API
      order for rows with missing/non-numeric internalDate.
    - Includes messages from first contact through the anchor message only.
    - Drops oldest message segments first until the transcript fits
      ``max_chars``; prepends an omission banner when any segments were dropped.
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be > 0, got {max_chars}.")

    thread = get_thread_full(thread_id)
    raw_messages = thread.get("messages", [])
    filtered = [
        msg
        for msg in raw_messages
        if "DRAFT" not in msg.get("labelIds", [])
    ]
    logger.info(
        "Thread %s: fetched %d message(s), %d non-draft message(s) after filtering.",
        thread_id,
        len(raw_messages),
        len(filtered),
    )

    ordered = _order_thread_messages(filtered)
    anchor_idx = next(
        (idx for idx, msg in enumerate(ordered) if msg.get("id") == anchor_message_id),
        None,
    )
    if anchor_idx is None:
        raise ValueError(
            "Anchor message not found in thread transcript after draft filtering "
            f"(thread_id={thread_id}, anchor_message_id={anchor_message_id})."
        )

    included = ordered[: anchor_idx + 1]
    segments = [_format_transcript_message(msg, idx + 1) for idx, msg in enumerate(included)]
    dropped = 0

    transcript = "\n\n".join(segments)
    while len(transcript) > max_chars and len(segments) > 1:
        segments.pop(0)
        dropped += 1
        transcript = "\n\n".join(segments)

    if len(transcript) > max_chars:
        # Pathological: a single message is longer than the entire budget.
        transcript = transcript[-max_chars:]
        dropped += 1

    if dropped > 0:
        banner = _THREAD_TRANSCRIPT_OMISSION_BANNER.format(count=len(segments))
        while segments and len(f"{banner}\n\n{transcript}") > max_chars and len(segments) > 1:
            segments.pop(0)
            dropped += 1
            banner = _THREAD_TRANSCRIPT_OMISSION_BANNER.format(count=len(segments))
            transcript = "\n\n".join(segments)

        transcript = f"{banner}\n\n{transcript}"
        if len(transcript) > max_chars:
            # If even a single segment + banner is too large, keep the banner
            # and clip the segment tail; the newest context is usually the
            # highest-value slice for drafting.
            head = f"{banner}\n\n"
            room = max(0, max_chars - len(head))
            transcript = f"{head}{transcript[len(head):][-room:]}"
        logger.info(
            "Thread %s: transcript truncated; dropped %d oldest message(s), retained %d message(s), %d char(s).",
            thread_id,
            dropped,
            len(segments),
            len(transcript),
        )
    else:
        logger.info(
            "Thread %s: transcript retained all %d message(s), %d char(s).",
            thread_id,
            len(segments),
            len(transcript),
        )

    return transcript


def _order_thread_messages(messages: list[dict]) -> list[dict]:
    """Order thread messages by timestamp, retaining undated rows.

    If every row has a parseable ``internalDate``, sort ascending by that
    timestamp. If *any* row has a missing/invalid date, fall back to the Gmail
    API order for the whole thread so undated rows keep their original
    conversation position.
    """
    parsed_dates = [_internal_date_ms(message) for message in messages]
    if any(value is None for value in parsed_dates):
        return list(messages)

    decorated = list(zip(parsed_dates, messages, strict=False))
    decorated.sort(key=lambda item: item[0])
    return [item[1] for item in decorated]


def _internal_date_ms(message: dict) -> int | None:
    """Return ``internalDate`` as epoch milliseconds, or ``None``."""
    raw = message.get("internalDate")
    if raw in (None, ""):
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def get_messages_metadata(
    message_ids: list[str],
    headers: tuple[str, ...] = ("Subject", "From"),
) -> list[dict]:
    """Batch-fetch header metadata for *message_ids* in chunked HTTP round-trips.

    The N+1 fix for list-style admin commands: instead of issuing one
    ``messages.get`` request per message, we bundle gets into multipart
    ``batch`` requests. Batches are chunked to ``_BATCH_CHUNK_SIZE`` because
    Gmail caps **concurrent requests per user** around 10 and the batch
    endpoint counts every sub-request against that cap simultaneously — a
    50-message batch reliably 429s the overflow. Per-id 429s are retried a
    small number of times with backoff before giving up.

    Returns a list of :func:`extract_message_parts`-shaped dicts in the same
    order as *message_ids*. Messages that still fail after retries are
    replaced with a stub dict containing the id and an ``error`` key, so
    the caller can surface the failure without having the whole list blow up.
    """
    if not message_ids:
        return []

    service = _build_service()
    results: dict[str, dict] = {}
    errors: dict[str, Exception] = {}

    def _execute_batch(ids: list[str]) -> None:
        """Run one batched Gmail request for *ids*; populate results/errors.

        Per-id failures land in ``errors`` via the callback; a whole-batch
        failure (network error, auth, etc.) re-raises so the caller sees a
        real exception instead of silently empty rows.
        """

        def _callback(
            request_id: str,
            response: dict | None,
            exception: Exception | None,
        ) -> None:
            if exception is not None:
                errors[request_id] = exception
            else:
                errors.pop(request_id, None)  # clear any prior 429
                results[request_id] = response or {}

        batch = service.new_batch_http_request(callback=_callback)
        for mid in ids:
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

    chunks = [
        message_ids[i : i + _BATCH_CHUNK_SIZE]
        for i in range(0, len(message_ids), _BATCH_CHUNK_SIZE)
    ]
    for chunk_index, chunk in enumerate(chunks):
        if chunk_index > 0 and _BATCH_INTER_CHUNK_SECONDS > 0:
            time.sleep(_BATCH_INTER_CHUNK_SECONDS)
        _execute_batch(chunk)

    # Retry whatever 429'd. We only retry rate-limit errors — auth, 404,
    # malformed-id failures won't recover by waiting.
    for attempt in range(1, _BATCH_MAX_RETRY_PASSES + 1):
        retry_ids = [mid for mid, exc in list(errors.items()) if _is_rate_limit(exc)]
        if not retry_ids:
            break
        delay = _BATCH_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
        logger.info(
            "Gmail batch: retrying %d rate-limited message(s) "
            "(pass %d/%d, after %.0fs).",
            len(retry_ids), attempt, _BATCH_MAX_RETRY_PASSES, delay,
        )
        time.sleep(delay)
        _execute_batch(retry_ids)

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
    # Normalize before the prefix check: incoming subjects occasionally carry
    # leading whitespace from line-folded headers, which would make a naive
    # ``startswith("re:")`` miss and produce ugly ``Re:  Re: ...`` subjects.
    normalized_subject = subject.strip()
    mime["Subject"] = (
        normalized_subject
        if normalized_subject.lower().startswith("re:")
        else f"Re: {normalized_subject}"
    )
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

def _received_at_display(message: dict) -> str:
    """Format Gmail ``internalDate`` (epoch ms as string) for operator UIs.

    Gmail always populates ``internalDate`` on ``messages.get`` responses,
    including ``format=metadata``. It reflects when Gmail received the
    message — stable for sorting and disambiguating same-subject threads.
    """
    raw = message.get("internalDate")
    if not raw:
        return ""
    try:
        ms = int(str(raw))
    except (TypeError, ValueError):
        return ""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_transcript_message(message: dict, ordinal: int) -> str:
    """Render one thread message segment for the LLM transcript block."""
    parts = extract_message_parts(message)
    received = parts["received_at"] or "(unknown time)"
    sender = parts["from_"] or "(unknown sender)"
    subject = parts["subject"] or "(no subject)"
    body = parts["body"] or "(no plain-text body)"
    return (
        f"--- MESSAGE {ordinal} ---\n"
        f"Received: {received}\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Body:\n{body}"
    )


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
    message_id_header (for In-Reply-To / References), received_at (UTC string).
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
        "received_at": _received_at_display(message),
    }
