"""Ollama LLM client.

All LLM calls go through the official `ollama` Python package using
Client(host=OLLAMA_BASE_URL). Model is set via OLLAMA_MODEL env var.

Public functions:
  - detect_language(text) -> str          returns "it", "es", or "en"
  - generate_reply(original, language) -> str
  - is_travel_related(subject, body) -> tuple[bool, str]   (travel?, language)
"""

from __future__ import annotations

import logging
import os

import ollama as ollama_sdk
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_CONTEXT = (
    "You are a helpful assistant for Wayonagio, a travel agency based in Cusco, Peru. "
    "Primary clients are Italian speakers, but also Spanish and English. "
    "Always be professional, friendly, and concise."
)


def _client() -> ollama_sdk.Client:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    return ollama_sdk.Client(host=base_url)


def _model() -> str:
    return os.environ.get("OLLAMA_MODEL", "llama3.2")


def _chat(messages: list[dict]) -> str:
    """Send a chat request and return the response content string."""
    try:
        response = _client().chat(model=_model(), messages=messages)
        return response.message.content.strip()
    except Exception as exc:
        logger.error(
            "Ollama request failed (model=%s, base_url=%s): %s. "
            "Ensure Ollama is running and OLLAMA_BASE_URL / OLLAMA_MODEL are set correctly.",
            _model(),
            os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
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
    """
    prompt = (
        "Detect the language of the following text. "
        "Reply with ONLY one of these codes: it, es, en. "
        "No explanation.\n\n"
        f"Text:\n{text[:500]}"
    )
    messages = [{"role": "user", "content": prompt}]
    raw = _chat(messages).lower().strip()

    # Accept the first recognised code found in the response
    for code in ("it", "es", "en"):
        if code in raw:
            return code

    logger.warning("detect_language returned unrecognised value %r, defaulting to 'en'.", raw)
    return "en"


def generate_reply(original: str, language: str) -> str:
    """Generate a travel-agency reply to *original* in *language*.

    *language* should be one of "it", "es", "en".
    """
    lang_names = {"it": "Italian", "es": "Spanish", "en": "English"}
    lang_name = lang_names.get(language, "English")

    messages = [
        {"role": "system", "content": _CONTEXT},
        {
            "role": "user",
            "content": (
                f"Write a professional reply in {lang_name} to the following email "
                "from a potential or existing client of our Cusco travel agency. "
                "Keep it concise and helpful. Do not add a subject line.\n\n"
                f"Original email:\n{original}"
            ),
        },
    ]
    return _chat(messages)


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
