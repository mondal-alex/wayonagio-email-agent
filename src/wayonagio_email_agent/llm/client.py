"""LLM client.

All LLM calls go through [LiteLLM](https://docs.litellm.ai/), which gives us a
single provider-agnostic interface. The provider is chosen by the ``LLM_MODEL``
env var using LiteLLM's standard ``provider/model`` naming, e.g.:

- ``ollama/llama3.2``          → self-hosted Ollama (local or remote)
- ``ollama/llama3.2:1b``       → Ollama with a specific tag
- ``gemini/gemini-2.5-flash``  → Google Gemini API

For backward compatibility, if ``LLM_MODEL`` is unset but ``OLLAMA_MODEL`` is
set, we fall back to ``ollama/<OLLAMA_MODEL>`` so existing deployments keep
working with no config change.

Provider-specific env vars:

- Ollama: ``OLLAMA_BASE_URL`` (default ``http://localhost:11434``),
  ``OLLAMA_KEEP_ALIVE`` (default ``1h``, forwarded to Ollama verbatim).
- Gemini: ``GEMINI_API_KEY`` (required when using a ``gemini/...`` model).

Public functions:
  - detect_language(text) -> str          returns "it", "es", or "en"
  - generate_reply(original, language) -> str
  - is_travel_related(subject, body) -> tuple[bool, str]   (travel?, language)
"""

from __future__ import annotations

import logging
import os
import re

import litellm

# Note: `.env` is loaded by the entry points (api.py, cli.py). Library modules
# intentionally don't call load_dotenv() so they stay cleanly importable in
# tests and from other apps without implicit filesystem reads.

logger = logging.getLogger(__name__)

# LiteLLM is chatty on import/first call; keep our logs clean.
litellm.suppress_debug_info = True

_CONTEXT = (
    "You are a helpful assistant for Wayonagio, a travel agency based in Cusco, Peru. "
    "Always be professional, friendly, and concise. "
    "You strictly follow language instructions: if the user says reply in a "
    "specific language, you reply in that language only and never switch."
)

_LANG_NAMES = {"it": "Italian", "es": "Spanish", "en": "English"}

_DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
_DEFAULT_KEEP_ALIVE = "1h"


def _model() -> str:
    """Resolve the LiteLLM model string.

    Priority: ``LLM_MODEL`` (preferred) > ``OLLAMA_MODEL`` (legacy, prefixed
    with ``ollama/``) > built-in default ``ollama/llama3.2``.
    """
    explicit = os.environ.get("LLM_MODEL", "").strip()
    if explicit:
        return explicit

    legacy = os.environ.get("OLLAMA_MODEL", "").strip()
    if legacy:
        return f"ollama/{legacy}"

    return "ollama/llama3.2"


def _provider(model: str) -> str:
    """Return the provider prefix (before the first slash), or "" if none."""
    return model.split("/", 1)[0] if "/" in model else ""


def _build_kwargs(messages: list[dict], options: dict | None) -> dict:
    """Build the kwargs dict for ``litellm.completion``.

    Honors provider-specific config so the rest of the code stays provider-
    agnostic. Standardized options (``temperature``, ``max_tokens``) are
    forwarded via LiteLLM's unified interface; provider-specific knobs (like
    Ollama's ``keep_alive``) are forwarded as extra kwargs.
    """
    model = _model()
    provider = _provider(model)
    opts = options or {}

    kwargs: dict = {"model": model, "messages": messages}

    if "temperature" in opts:
        kwargs["temperature"] = opts["temperature"]
    if "max_tokens" in opts:
        kwargs["max_tokens"] = opts["max_tokens"]

    if provider == "ollama":
        kwargs["api_base"] = os.environ.get(
            "OLLAMA_BASE_URL", _DEFAULT_OLLAMA_BASE_URL
        )
        kwargs["keep_alive"] = os.environ.get(
            "OLLAMA_KEEP_ALIVE", _DEFAULT_KEEP_ALIVE
        )
    elif provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "LLM_MODEL is set to a Gemini model but GEMINI_API_KEY is not set. "
                "Set GEMINI_API_KEY in your environment / .env / Secret Manager."
            )
        kwargs["api_key"] = api_key

    return kwargs


def _chat(messages: list[dict], options: dict | None = None) -> str:
    """Send a chat request via LiteLLM and return the response content.

    Logs a warning if the provider reports ``finish_reason == "length"``, i.e.
    the reply was cut off by ``max_tokens``. That's the only way a reply gets
    silently truncated, and it produces incomplete drafts that look unpolished
    when the staff opens them — worth being loud about.
    """
    try:
        response = litellm.completion(**_build_kwargs(messages, options))
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            logger.warning(
                "LLM reply was truncated by max_tokens (finish_reason=length). "
                "The draft may be incomplete; consider raising max_tokens."
            )
        return (choice.message.content or "").strip()
    except Exception as exc:
        model = _model()
        logger.error(
            "LLM request failed (model=%s): %s. "
            "Check that the provider credentials are configured (GEMINI_API_KEY "
            "for Gemini, or that Ollama is running at OLLAMA_BASE_URL).",
            model,
            exc,
        )
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_language(text: str) -> str:
    """Detect the primary language of *text*.

    Returns a BCP-47 language code limited to "it", "es", or "en".
    Defaults to "en" if the response is unrecognised.

    Parsing is deliberately strict to avoid false positives — e.g. a stray
    "It is English" response must not be read as Italian. We check, in order:

    1. The whole response is exactly one of the codes.
    2. The *first line* is exactly one of the codes.
    3. The first line contains one of the codes as a standalone word.

    Anything else falls through to the "en" default.
    """
    prompt = (
        "Detect the language of the following text. "
        "Reply with ONLY one of these codes: it, es, en. "
        "No explanation.\n\n"
        f"Text:\n{text[:500]}"
    )
    messages = [{"role": "user", "content": prompt}]
    raw = _chat(messages).lower().strip()

    if raw in _LANG_NAMES:
        return raw

    first_line = raw.splitlines()[0].strip(" \t.:;,!?\"'") if raw else ""
    if first_line in _LANG_NAMES:
        return first_line

    match = re.search(r"\b(it|es|en)\b", first_line)
    if match:
        return match.group(1)

    logger.warning("detect_language returned unrecognised value %r, defaulting to 'en'.", raw)
    return "en"


class EmptyReplyError(RuntimeError):
    """Raised when the LLM returns an empty or whitespace-only reply.

    We refuse to create an empty draft — it would look worse than not drafting
    at all, and it usually indicates a broken provider config or rate limit.
    """


def generate_reply(original: str, language: str) -> str:
    """Generate a travel-agency reply to *original* in *language*.

    *language* should be one of "it", "es", "en". Raises :class:`EmptyReplyError`
    if the LLM returns an empty or whitespace-only string so the caller can
    decline to draft rather than silently creating a blank email.

    When the optional knowledge base is enabled (``KB_ENABLED=true`` and an
    index is available), the top-k retrieved chunks are inserted as a
    clearly-delimited ``REFERENCE MATERIAL`` section. If retrieval fails, we
    silently fall back to the base prompt rather than refuse to draft.
    """
    from wayonagio_email_agent.kb import retrieve as kb_retrieve

    lang_name = _LANG_NAMES.get(language, "English")

    try:
        hits = kb_retrieve.retrieve(original)
    except Exception as exc:  # noqa: BLE001
        logger.warning("KB retrieval unavailable: %s", exc)
        hits = []
    reference_block = kb_retrieve.format_reference_block(hits) if hits else ""

    user_content = (
        f"LANGUAGE REQUIREMENT: Write your entire reply in {lang_name}. "
        f"Do not use Spanish, English, or any language other than {lang_name} "
        "(unless that language is the one requested). "
        "Even if the original email is in a different language, your reply "
        f"must be in {lang_name}.\n\n"
        "TASK: Write a professional, concise reply from our Cusco travel agency "
        "to the client email below. Do not add a subject line. Do not include "
        "any meta commentary or translations — output only the reply body.\n\n"
    )
    if reference_block:
        user_content += (
            "USE OF REFERENCE MATERIAL: When the client asks about specific "
            "facts — prices, inclusions, durations, policies — only use facts "
            "that appear in the reference material below. If a fact is not in "
            "the reference material, do not invent it; ask the client for "
            "clarification or offer to follow up.\n\n"
            f"{reference_block}\n\n"
        )
    user_content += f"CLIENT EMAIL:\n{original}\n\nYOUR REPLY (in {lang_name}):"

    messages = [
        {"role": "system", "content": _CONTEXT},
        {"role": "user", "content": user_content},
    ]
    reply = _chat(messages, options={"temperature": 0.4, "max_tokens": 800})
    if not reply.strip():
        raise EmptyReplyError(
            "LLM returned an empty reply; refusing to create a blank draft."
        )
    return reply


def is_travel_related(subject: str, body: str) -> tuple[bool, str]:
    """Classify whether an email is travel-related.

    Returns (is_related: bool, language_code: str).
    Language code is one of "it", "es", "en".

    Intentionally simple — one short prompt, yes/no + language code.
    """
    prompt = (
        "Is the following email related to travel, tours, or trips to Peru or Cusco? "
        "Reply with exactly two tokens separated by a space: "
        "first 'yes' or 'no', then the language code ('it', 'es', or 'en'). "
        "Example: 'yes it'  or  'no en'\n\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{body[:800]}"
    )
    messages = [{"role": "user", "content": prompt}]
    raw = _chat(messages).lower().strip()

    parts = raw.split()
    related = parts[0].startswith("y") if parts else False
    language = "en"
    for code in ("it", "es", "en"):
        if code in parts[1:]:
            language = code
            break

    logger.debug("is_travel_related → related=%s lang=%s (raw=%r)", related, language, raw)
    return related, language
