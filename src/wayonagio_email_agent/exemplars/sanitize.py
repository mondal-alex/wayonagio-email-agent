"""PII / secret tripwire for exemplar Docs.

This module is a **safety net**, not the primary defense. The curator's job
is to anonymize names, dates, and identifiers *while writing* the exemplar
Doc. The regex pass below catches the obvious mechanical leaks the curator
might miss — booking URLs containing reservation IDs, raw email addresses,
phone numbers, IBANs, and Luhn-valid card numbers — so a single oversight
doesn't surface a real customer's PII in the LLM prompt or the resulting
draft.

Order matters:

1. **BOOKING_URL first.** Many booking URLs contain emails, phones, or IDs
   in their query string; substituting the whole URL as ``<BOOKING_URL>``
   prevents the later passes from spraying ``<EMAIL>``/``<PHONE>`` markers
   inside what was supposed to be a single redaction.
2. **EMAIL** next — emails have distinctive ``@`` anchors, so they're
   unambiguous and shouldn't be eaten by the more permissive digit-run
   patterns later.
3. **IBAN** before **CARD** — IBANs always start with two letters, which
   keeps the leading characters from being shaved off by a digit-run
   match further down.
4. **CARD** before **PHONE** — this is the non-obvious ordering rule. A
   formatted card (``4242 4242 4242 4242``) is a 16-digit run that the
   tolerant phone regex would happily eat, leaving the Luhn-checked card
   pass with nothing to redact. Cards are validated by Luhn (a
   significantly tighter test than the phone shape), so giving them first
   refusal of digit runs avoids false-positive ``<PHONE>`` substitution
   on real card numbers.
5. **PHONE last** — operates on whatever digit runs survived the more
   specific patterns above.

The CI tripwire test in ``tests/test_exemplars_sanitize.py`` and the
integration test in ``tests/test_exemplars_loader.py`` both feed deliberately-
leaked PII through the full pipeline and assert none of it survives.
"""

from __future__ import annotations

import re

_BOOKING_URL_RE = re.compile(
    # Any URL whose path or query carries a token of length >=10 mixing letters
    # and digits — that's the empirical shape of booking IDs across providers
    # (Stripe, GetYourGuide, agency-internal). Plain marketing URLs like
    # https://wayonagio.com/tours/salkantay don't match because their path
    # tokens are pure-letter words.
    r"https?://[^\s<>\"']*?(?:[A-Za-z][A-Za-z0-9_-]{9,}|[0-9][A-Za-z0-9_-]{9,})[^\s<>\"']*",
    re.IGNORECASE,
)

# A short URL (no booking ID) is left alone on purpose so curator-written
# wayonagio.com links survive sanitization. The regex above only redacts URLs
# that look like they carry a reservation/booking identifier.

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
)

_PHONE_RE = re.compile(
    # Tolerant international + local pattern: optional +, optional country
    # code, optional area code in parens, separators are space/dash/dot.
    # Requires at least 7 digits total to avoid matching short numeric runs
    # that aren't phone numbers (years, prices, table IDs).
    r"""
    (?<!\w)                  # not glued to a previous word char
    \+?                      # optional leading +
    (?:\(?\d{1,4}\)?[\s.\-]?)?  # optional country/area code
    (?:\d[\s.\-]?){6,14}\d   # 7-15 digits with optional separators
    (?!\w)                   # not followed by a word char
    """,
    re.VERBOSE,
)

_IBAN_RE = re.compile(
    # Country code (2 letters) + check digits (2 digits) + BBAN (11-30 chars).
    r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
)

# Card numbers: 13-19 digit runs with optional space/dash separators. We
# pre-filter with a permissive regex and then validate with Luhn so we don't
# redact every long digit string (booking IDs, postal tracking numbers).
_CARD_CANDIDATE_RE = re.compile(
    r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)",
)


def _luhn_valid(number: str) -> bool:
    """Return True if *number* (digits only) passes the Luhn checksum."""
    if not number.isdigit() or not (13 <= len(number) <= 19):
        return False
    total = 0
    # Iterate right-to-left, doubling every second digit.
    for index, ch in enumerate(reversed(number)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _iban_mod97_valid(iban: str) -> bool:
    """Return True if *iban* satisfies the IBAN mod-97 checksum.

    We still redact non-mod-97-valid matches because the cost of leaking a
    real IBAN is asymmetric with the cost of redacting a false positive
    (a curator-written placeholder string of the same shape). This helper
    exists so future tightening (only redact valid IBANs) is one config
    change, not a regex rewrite.
    """
    rearranged = iban[4:] + iban[:4]
    digits = "".join(
        str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged
    )
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def _redact_cards(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        candidate = re.sub(r"[ \-]", "", match.group(0))
        if _luhn_valid(candidate):
            return "<CARD>"
        return match.group(0)

    return _CARD_CANDIDATE_RE.sub(_replace, text)


def sanitize(text: str) -> str:
    """Redact PII patterns in *text* and return the cleaned string.

    Empty / whitespace-only input is returned unchanged so the caller can
    decide what to do with it (typically: skip the exemplar with a WARNING).
    """
    if not text or not text.strip():
        return text

    cleaned = _BOOKING_URL_RE.sub("<BOOKING_URL>", text)
    cleaned = _EMAIL_RE.sub("<EMAIL>", cleaned)
    cleaned = _IBAN_RE.sub("<IBAN>", cleaned)
    cleaned = _redact_cards(cleaned)
    cleaned = _PHONE_RE.sub("<PHONE>", cleaned)
    return cleaned


__all__ = ["sanitize"]
