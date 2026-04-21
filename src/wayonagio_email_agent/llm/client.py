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
- Optional: ``LLM_MAX_REPLY_TOKENS`` — output budget for ``generate_reply``
  (default 8192). Gemini 2.5 models count **thinking** tokens against the
  same cap as visible text; 800 was enough to produce mid-sentence cutoffs.
- Optional: ``LLM_CHAT_MAX_RETRIES`` — how many times to retry a **transient**
  chat failure (503/502/500, connection errors, 429) per call with exponential
  backoff before surfacing the error (default 5).

Public functions:
  - detect_language(text) -> str          returns "it", "es", or "en"
  - generate_reply(thread_transcript, subject, language, latest_customer_turn?) -> str
  - is_travel_related(subject, body) -> tuple[bool, str]   (travel?, language)
"""

from __future__ import annotations

import logging
import os
import re
import time

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

# Draft replies can be several paragraphs (Italian formal closings, multiple
# Q&A). Gemini 2.5 Flash/Pro also spend part of max_output_tokens on internal
# "thinking" — a low cap yields finish_reason=length and staff see drafts
# ending mid-word (e.g. after "Per,"). Keep a generous default; operators
# can lower via LLM_MAX_REPLY_TOKENS if a provider complains.
_DEFAULT_REPLY_MAX_TOKENS = 8192
_MIN_REPLY_MAX_TOKENS = 1024
_KB_QUERY_THREAD_TAIL_CHARS = 4_000
_LANGUAGE_SAMPLE_HEAD_CHARS = 2_000
_LANGUAGE_SAMPLE_TAIL_CHARS = 2_000
_TRAVEL_CLASSIFIER_BODY_CHARS = 8_000

# Transient errors on ``litellm.completion`` (Gemini "high demand" 503, etc.).
# Retrying is safe: we only repeat a stateless chat request with no server-
# side partial state. Auth, bad requests, and context errors are not retried.
_DEFAULT_CHAT_TRANSIENT_RETRIES = 5
_CHAT_TRANSIENT_INITIAL_BACKOFF_SECONDS = 3.0
_CHAT_TRANSIENT_MAX_BACKOFF_SECONDS = 60.0

_TRANSIENT_CHAT_ERRORS: tuple[type[Exception], ...] = (
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.BadGatewayError,
    litellm.APIConnectionError,
    litellm.RateLimitError,
)


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


def _reply_max_tokens() -> int:
    """LiteLLM ``max_tokens`` for ``generate_reply`` (thinking + visible text on Gemini 2.5)."""
    raw = os.environ.get("LLM_MAX_REPLY_TOKENS", "").strip()
    if not raw:
        return _DEFAULT_REPLY_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "LLM_MAX_REPLY_TOKENS=%r is not an integer; using default %d.",
            raw,
            _DEFAULT_REPLY_MAX_TOKENS,
        )
        return _DEFAULT_REPLY_MAX_TOKENS
    if value < _MIN_REPLY_MAX_TOKENS:
        logger.warning(
            "LLM_MAX_REPLY_TOKENS=%d is below minimum %d; clamping.",
            value,
            _MIN_REPLY_MAX_TOKENS,
        )
        return _MIN_REPLY_MAX_TOKENS
    return value


def _max_chat_transient_retries() -> int:
    """How many backoff retries after a failed ``completion`` attempt."""
    raw = os.environ.get("LLM_CHAT_MAX_RETRIES", "").strip()
    if not raw:
        return _DEFAULT_CHAT_TRANSIENT_RETRIES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "LLM_CHAT_MAX_RETRIES=%r is not an integer; using default %d.",
            raw,
            _DEFAULT_CHAT_TRANSIENT_RETRIES,
        )
        return _DEFAULT_CHAT_TRANSIENT_RETRIES
    return max(0, value)


def _language_sample(text: str) -> str:
    """Condense long thread text for language detection prompts."""
    if len(text) <= (_LANGUAGE_SAMPLE_HEAD_CHARS + _LANGUAGE_SAMPLE_TAIL_CHARS):
        return text
    head = text[:_LANGUAGE_SAMPLE_HEAD_CHARS]
    tail = text[-_LANGUAGE_SAMPLE_TAIL_CHARS :]
    return (
        f"{head}\n\n[... earlier thread content omitted for language detection ...]\n\n{tail}"
    )


def _kb_query_from_thread(
    *,
    transcript: str,
    subject: str,
    latest_customer_turn: str | None = None,
) -> str:
    """Build a KB retrieval query with latest turn priority.

    Retrieval should primarily track what the customer is asking *now*, while
    still seeing nearby context for disambiguation.
    """
    latest = latest_customer_turn.strip() if latest_customer_turn else ""
    if not latest:
        latest = transcript[-_KB_QUERY_THREAD_TAIL_CHARS :]
    recent = transcript[-_KB_QUERY_THREAD_TAIL_CHARS :]
    return (
        f"Subject: {subject}\n\n"
        f"Latest customer turn:\n{latest}\n\n"
        f"Recent thread context:\n{recent}"
    ).strip()


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

    Retries transient provider failures (503 UNAVAILABLE, 502, 500, connection
    errors, 429) with exponential backoff — see ``LLM_CHAT_MAX_RETRIES``.

    Logs a warning if the provider reports ``finish_reason == "length"``, i.e.
    the reply was cut off by ``max_tokens``. That's the only way a reply gets
    silently truncated, and it produces incomplete drafts that look unpolished
    when the staff opens them — worth being loud about.
    """
    max_retries = _max_chat_transient_retries()
    attempt = 0
    model = _model()

    while True:
        try:
            response = litellm.completion(**_build_kwargs(messages, options))
            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "length":
                logger.warning(
                    "LLM reply was truncated (finish_reason=length): output hit the "
                    "max_tokens budget — the draft may end mid-sentence. Gemini 2.5 "
                    "models count internal thinking tokens in the same budget; "
                    "raise LLM_MAX_REPLY_TOKENS (default %d) or shorten prompts.",
                    _DEFAULT_REPLY_MAX_TOKENS,
                )
            return (choice.message.content or "").strip()
        except _TRANSIENT_CHAT_ERRORS as exc:
            if attempt >= max_retries:
                logger.error(
                    "LLM request failed after %d transient-error retries (model=%s): %s",
                    max_retries,
                    model,
                    exc,
                )
                raise
            delay = min(
                _CHAT_TRANSIENT_INITIAL_BACKOFF_SECONDS * (2**attempt),
                _CHAT_TRANSIENT_MAX_BACKOFF_SECONDS,
            )
            logger.warning(
                "LLM transient error (%s, model=%s); retry %d/%d after %.0fs: %s",
                type(exc).__name__,
                model,
                attempt + 1,
                max_retries,
                delay,
                exc,
            )
            time.sleep(delay)
            attempt += 1
        except Exception as exc:
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
        f"Text:\n{_language_sample(text)}"
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


def generate_reply(
    *,
    thread_transcript: str,
    subject: str,
    language: str,
    latest_customer_turn: str | None = None,
) -> str:
    """Generate a travel-agency reply to *thread_transcript* in *language*.

    *language* should be one of "it", "es", "en". Raises :class:`EmptyReplyError`
    if the LLM returns an empty or whitespace-only string so the caller can
    decline to draft rather than silently creating a blank email.

    The knowledge base is **required**. The top-k retrieved chunks are
    inserted as a clearly-delimited ``REFERENCE MATERIAL`` section so the
    model can ground specific facts (prices, inclusions, durations, policies)
    in agency content rather than hallucinate them. If KB retrieval fails
    (artifact missing, embedding API down, model mismatch), the exception
    propagates and no draft is created — an ungrounded draft that staff might
    send unmodified is worse than no draft at all.

    The exemplars subsystem (``exemplars/``) is **optional and graceful**:
    when enabled and populated, the curator-managed example replies are
    appended *after* the REFERENCE MATERIAL block as ``EXAMPLE RESPONSES``,
    with explicit framing that the KB wins on facts. When disabled or
    empty, the EXAMPLE RESPONSES block is omitted entirely so the prompt
    stays minimal. Exemplar load failures never block drafting — the
    loader returns ``[]`` and we proceed with KB-only grounding.
    """
    from wayonagio_email_agent.exemplars import loader as exemplar_loader
    from wayonagio_email_agent.exemplars import prompt as exemplar_prompt
    from wayonagio_email_agent.kb import retrieve as kb_retrieve

    lang_name = _LANG_NAMES.get(language, "English")
    latest_turn = latest_customer_turn.strip() if latest_customer_turn else ""
    if not latest_turn:
        latest_turn = thread_transcript

    hits = kb_retrieve.retrieve(
        _kb_query_from_thread(
            transcript=thread_transcript,
            subject=subject,
            latest_customer_turn=latest_turn,
        )
    )
    reference_block = kb_retrieve.format_reference_block(hits) if hits else ""

    # Belt-and-suspenders: ``exemplar_loader.get_all_exemplars`` is contracted
    # to never raise (it catches all collection errors and caches ``[]``).
    # We still wrap the call here so the "exemplars never block a draft"
    # invariant holds even if a future regression weakens the loader's
    # safety net — e.g. someone narrows the except clause and a new error
    # type slips through. The KB stays fail-loud above; only exemplars
    # degrade silently.
    try:
        exemplars = exemplar_loader.get_all_exemplars()
    except Exception as exc:  # noqa: BLE001 — exemplars are optional + graceful
        logger.warning(
            "Exemplar loader violated its never-raises contract; "
            "drafting will continue with KB-only grounding: %s",
            exc,
            exc_info=True,
        )
        exemplars = []
    exemplar_block = exemplar_prompt.format_exemplar_block(exemplars)

    user_content = (
        f"LANGUAGE REQUIREMENT: Write your entire reply in {lang_name}. "
        f"Do not use Spanish, English, or any language other than {lang_name} "
        "(unless that language is the one requested). "
        "Even if the original email is in a different language, your reply "
        f"must be in {lang_name}.\n\n"
        "TASK: Write a professional, concise reply from our Cusco travel agency "
        "to the client email thread below. "
        "Do not add a subject line. Do not include "
        "any meta commentary or translations — output only the reply body.\n\n"
        "RESPONSE SCOPE:\n"
        "- Answer the latest customer turn below as the primary task.\n"
        "- Use earlier thread messages only as background context.\n"
        "- Do NOT re-answer questions that were already resolved earlier in the "
        "thread unless the latest customer turn explicitly asks to revisit them.\n\n"
        "LATEST CUSTOMER TURN (primary task):\n"
        f"{latest_turn}\n\n"
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
    # Exemplars come AFTER the reference block on purpose: the framing in
    # ``format_exemplar_block`` says "the REFERENCE MATERIAL above is
    # authoritative", and "above" only reads correctly if the order on the
    # page matches the words. Don't reorder these two blocks without also
    # editing exemplars/prompt.py.
    #
    # We additionally only inject exemplars when the reference block is
    # present. This is two safety properties in one:
    #
    # 1. The exemplar framing literally says "REFERENCE MATERIAL above" —
    #    without a reference block, that instruction is incoherent and
    #    could confuse the model.
    # 2. Exemplars without KB grounding are dangerous: the model may copy
    #    example facts (prices, durations) verbatim with no canonical
    #    source to override them. The KB is the only thing standing
    #    between examples and stale facts in the draft.
    #
    # In practice the KB is required and almost always returns hits, so
    # this branch nearly always fires; the guard exists for the
    # pathological case (`top_k=0`, an index with zero chunks) so the
    # prompt stays coherent rather than self-contradictory.
    if exemplar_block and reference_block:
        user_content += f"{exemplar_block}\n\n"
    user_content += (
        "CLIENT EMAIL THREAD (chronological, oldest first):\n"
        f"{thread_transcript}\n\n"
        f"YOUR REPLY (in {lang_name}):"
    )

    messages = [
        {"role": "system", "content": _CONTEXT},
        {"role": "user", "content": user_content},
    ]
    reply = _chat(
        messages,
        options={"temperature": 0.4, "max_tokens": _reply_max_tokens()},
    )
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
        f"Body:\n{body[:_TRAVEL_CLASSIFIER_BODY_CHARS]}"
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
