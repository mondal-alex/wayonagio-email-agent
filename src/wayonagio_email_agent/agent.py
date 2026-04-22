"""Agent orchestration.

Manual draft flow (used by CLI and API):
    manual_draft_flow(message_id) -> dict
    Fetch → detect language → generate reply → create draft

Automatic scanner (two entry points over the same logic):
    scan_once(dry_run)             one pass, returns and exits.
                                   Use this with external schedulers
                                   (Cloud Run Jobs + Cloud Scheduler,
                                   cron, systemd timers).

    scan_loop(interval, dry_run)   long-running ``while True`` loop.
                                   Use this only in always-on environments
                                   (a VM, a long-running container, etc.) —
                                   not on Cloud Run, which scales idle
                                   instances to zero.

Both entry points share ``_process_message``: failures on individual messages
are isolated so one bad email never stops the batch.
"""

from __future__ import annotations

import logging
import os
import time

from wayonagio_email_agent import gmail_client, state
from wayonagio_email_agent.llm import client as llm

logger = logging.getLogger(__name__)
_DEFAULT_THREAD_MAX_CHARS = 48_000
_MIN_THREAD_MAX_CHARS = 1_000


def scanner_enabled() -> bool:
    """Return whether automatic scanning is enabled via configuration."""
    value = os.environ.get("SCANNER_ENABLED", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _thread_max_chars() -> int:
    """Resolve ``LLM_THREAD_MAX_CHARS`` with defaults and minimum clamp."""
    raw = os.environ.get("LLM_THREAD_MAX_CHARS", "").strip()
    if not raw:
        return _DEFAULT_THREAD_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "LLM_THREAD_MAX_CHARS=%r is not an integer; using default %d.",
            raw,
            _DEFAULT_THREAD_MAX_CHARS,
        )
        return _DEFAULT_THREAD_MAX_CHARS
    if value < _MIN_THREAD_MAX_CHARS:
        logger.warning(
            "LLM_THREAD_MAX_CHARS=%d is below minimum %d; clamping.",
            value,
            _MIN_THREAD_MAX_CHARS,
        )
        return _MIN_THREAD_MAX_CHARS
    return value


def manual_draft_flow(message_id: str, forced_language: str | None = None) -> dict:
    """Create a draft reply for *message_id*.

    Used by both the API (Add-on trigger) and the CLI `draft-reply` command.
    No travel classification — caller has already chosen the email.

    Returns the Gmail draft resource dict.
    """
    logger.info("Starting manual draft flow for message %s.", message_id)

    message = gmail_client.get_message(message_id)
    parts = gmail_client.extract_message_parts(message)
    # Gmail add-ons may pass a client-style id (e.g. msg-f:...); thread payloads
    # use the API's canonical id, so anchor transcript lookup on message["id"].
    anchor_id = message["id"]
    transcript = gmail_client.build_thread_transcript(
        thread_id=parts["thread_id"],
        anchor_message_id=anchor_id,
        max_chars=_thread_max_chars(),
    )

    body_text = transcript
    if forced_language:
        language = forced_language
        logger.debug("Using forced language from caller: %s", language)
    else:
        language = llm.detect_language(body_text)
        logger.debug("Detected language: %s", language)

    reply_body = llm.generate_reply(
        thread_transcript=transcript,
        subject=parts["subject"],
        language=language,
        latest_customer_turn=parts["body"] or parts["subject"],
    )

    draft = gmail_client.draft_reply(
        thread_id=parts["thread_id"],
        to=parts["from_"],
        subject=parts["subject"],
        body=reply_body,
        in_reply_to=parts["message_id_header"],
        references=_build_references(parts["references"], parts["message_id_header"]),
    )
    logger.info("Draft created successfully for message %s.", message_id)
    return draft


def scan_loop(interval: int = 1800, dry_run: bool = False) -> None:
    """Run the automatic scanner indefinitely.

    Each iteration:
    1. Fetches recent unread emails.
    2. Skips already-processed messages (SQLite state).
    3. Classifies with is_travel_related().
    4. Skips if not travel-related.
    5. Secondary dedup: skips if the Gmail thread already has a draft.
    6. Creates a draft (or logs [DRY RUN] if dry_run=True).
    7. Marks message as processed in state DB only after successful draft.

    Errors on individual messages are caught and logged; the loop continues.
    """
    mode = "[DRY RUN] " if dry_run else ""
    logger.info("%sScanner started. Interval: %ds.", mode, interval)

    while True:
        try:
            scan_once(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in scan iteration: %s", exc, exc_info=True)

        logger.debug("Sleeping %ds until next scan.", interval)
        time.sleep(interval)


def scan_once(dry_run: bool = False) -> None:
    """Run a single scan pass over recent unread messages and return.

    This is the one-shot entry point; it's what external schedulers invoke.
    Per-message failures are caught and logged so a single bad message never
    aborts the pass.
    """
    messages = gmail_client.list_messages(q="is:unread", max_results=50)
    if not messages:
        logger.debug("No unread messages found.")
        return

    logger.info("Found %d unread message(s).", len(messages))

    for msg_meta in messages:
        message_id = msg_meta["id"]
        try:
            _process_message(message_id, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to process message %s: %s", message_id, exc, exc_info=True
            )


def _process_message(message_id: str, dry_run: bool) -> None:
    # Primary dedup: already processed in a previous run?
    if state.is_processed(message_id):
        logger.debug("Message %s already processed, skipping.", message_id)
        return

    message = gmail_client.get_message(message_id)
    parts = gmail_client.extract_message_parts(message)
    anchor_id = message["id"]
    transcript = gmail_client.build_thread_transcript(
        thread_id=parts["thread_id"],
        anchor_message_id=anchor_id,
        max_chars=_thread_max_chars(),
    )

    related, language = llm.is_travel_related(
        subject=parts["subject"], body=transcript
    )
    if not related:
        logger.debug("Message %s is not travel-related, skipping.", message_id)
        state.mark_processed(message_id, outcome="non_travel")
        return

    # Secondary dedup: thread already has a draft in Gmail?
    if gmail_client.thread_has_draft(parts["thread_id"]):
        logger.info(
            "Thread %s already has a draft, skipping message %s.",
            parts["thread_id"],
            message_id,
        )
        state.mark_processed(message_id, outcome="thread_has_draft")
        return

    reply_body = llm.generate_reply(
        thread_transcript=transcript,
        subject=parts["subject"],
        language=language,
        latest_customer_turn=parts["body"] or parts["subject"],
    )

    if dry_run:
        logger.info(
            "[DRY RUN] Would create draft for message %s (lang=%s, thread=%s).",
            message_id,
            language,
            parts["thread_id"],
        )
        return

    gmail_client.draft_reply(
        thread_id=parts["thread_id"],
        to=parts["from_"],
        subject=parts["subject"],
        body=reply_body,
        in_reply_to=parts["message_id_header"],
        references=_build_references(parts["references"], parts["message_id_header"]),
    )
    state.mark_processed(message_id, outcome="drafted")


def _build_references(existing: str, message_id_header: str) -> str:
    """Append *message_id_header* to the References chain.

    Internal whitespace is collapsed: real-world headers occasionally come
    back with multiple spaces between message IDs (line-folded headers,
    upstream reformatting), and emitting those verbatim produces ugly
    ``References:`` lines that some MUAs render with visible gaps.
    """
    if not existing:
        return message_id_header
    parts = existing.split() + [message_id_header]
    return " ".join(parts)
