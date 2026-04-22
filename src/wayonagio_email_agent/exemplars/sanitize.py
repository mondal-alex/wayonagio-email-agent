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
from collections.abc import Iterable

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


def _dedupe_phrases_preserve_order(phrases: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in phrases:
        phrase = (raw or "").strip()
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        out.append(phrase)
    return out


def redact_phrase_map(text: str, pairs: list[tuple[str, str]]) -> str:
    """Replace each *original* string with a specific *replacement*.

    *pairs* is ``(original, replacement)``; longer originals are applied
    first so a short phrase does not split a still-present longer one.

    Matching is case-insensitive for *original*; ``re.escape`` is used.
    """
    if not pairs:
        return text
    sorted_pairs = sorted(
        ((p, r) for p, r in pairs if (p or "").strip()),
        key=lambda t: len(t[0]),
        reverse=True,
    )
    for phrase, repl in sorted_pairs:
        pat = re.compile(re.escape(phrase), re.IGNORECASE)
        text = pat.sub(repl, text)
    return text


def redact_listed_phrases(
    text: str,
    phrases: Iterable[str],
    *,
    placeholder: str = "<NAME>",
) -> str:
    """Replace *phrases* (person names, etc.) with *placeholder*.

    Does **not** try to discover names — pass explicit strings, e.g. from
    ``--name`` or a list file. Longer phrases are applied first so
    substrings do not break multi-word names.

    Matching is case-insensitive; re ``re.escape`` is used so special regex
    characters in names (``O'Brien``) are safe.
    """
    unique = _dedupe_phrases_preserve_order(phrases)
    if not unique:
        return text
    pairs = [(p, placeholder) for p in unique]
    return redact_phrase_map(text, pairs)


def _looks_like_gmail_message_header_line(line: str) -> bool:
    """Heuristic for a single-line Gmail (and similar) *From* / header bar."""
    s = line.strip()
    if len(s) < 30:
        return False
    if "<" not in s or ">" not in s:
        return False
    # English: "On Mon, 1 Jan 2026, x wrote:" (single line)
    if re.match(r"^On .+wrote:?\s*$", s, re.IGNORECASE) and len(s) < 2000:
        return True
    if re.match(r"^----+\s*Original Message\s*----", s, re.IGNORECASE):
        return True
    # Spanish / it-ES exports:  "Name <a@b> 23 de marzo de 2026 a las 2:58 p.m. Para: ..."
    if s.count(" de ") >= 2 and re.search(
        r"\d{1,2} de [a-záéíóúüñA-ZÁÉÍÓÚÜÑ\.\- ]+ de 20\d{2}", s, re.IGNORECASE
    ):
        if re.search(
            r"(a las|a\.\s*m\.|p\.\s*m\.|Para:)", s, re.IGNORECASE
        ) or re.search(
            r">\s+\d{1,2} de", s
        ):  # mail token then day
            return True
    return False


def _join_wrapped_gmail_from_lines(lines: list[str]) -> list[str]:
    r"""Rejoin a *From* / header that Google *Docs* or soft wraps split across lines.

    Plain-text export of a single paragraph can still be one line; if the
    user broke the header with hard returns, ``_looks_like…`` fails on each
    line alone. Merge *line* + *next* when the merge matches and the first
    part does not, and ``<`` appears in the first part (email-shaped).
    """
    if len(lines) < 2:
        return lines
    out: list[str] = list(lines)
    changed = True
    # Repeat in case a header was split 3+ ways.
    for _ in range(8):
        if not changed:
            break
        changed = False
        nxt: list[str] = []
        i = 0
        while i < len(out):
            if i < len(out) - 1:
                a, b = out[i].strip(), out[i + 1].strip()
                merged = (out[i] + " " + out[i + 1]).strip()
                if (
                    "<" in a
                    and (not _looks_like_gmail_message_header_line(a))
                    and _looks_like_gmail_message_header_line(merged)
                ):
                    nxt.append(merged)
                    i += 2
                    changed = True
                    continue
            nxt.append(out[i])
            i += 1
        out = nxt
    return out


def _elide_repeated_gmail_print_headers(lines: list[str]) -> list[str]:
    r"""Gmail *print* / *save as* often repeats a title line (e.g. ``Correo de…``).

    That breaks visual flow. Collapse each run into one short marker line.
    """
    out: list[str] = []
    prev_matched = False
    for line in lines:
        s = line.strip()
        if len(s) > 12 and s.lower().startswith("correo de "):
            if re.search(
                r"\b(PM|AM|p\.m\.|a\.m\.)\.?\s*$", s, re.IGNORECASE
            ) and re.search(r"[\d/]+", s):
                if not prev_matched:
                    out.append("--- (PDF/print page header, duplicate removed) ---")
                prev_matched = True
                continue
        prev_matched = False
        out.append(line)
    return out


def mark_message_boundaries_in_export(s: str) -> str:
    r"""Insert visible separators before lines that look like a new *email* header.

    Tuned for plain-text thread exports (Gmail *print* / *save* as *text*),
    not the running agent. Safe to use on any paste: lines that are not
    *From*-style pass through unchanged.
    """
    if not s.strip():
        return s
    lines = s.split("\n")
    out: list[str] = []
    msg_num = 0
    for line in lines:
        if _looks_like_gmail_message_header_line(line):
            if msg_num > 0:
                out.append("")
                out.append(76 * "-")
                out.append(f"  Message {msg_num + 1}")
                out.append(76 * "-")
                out.append("")
            msg_num += 1
        out.append(line)
    return "\n".join(out)


def tidy_exemplar_export(
    s: str,
    *,
    elide_print_titles: bool = True,
    mark_messages: bool = True,
) -> str:
    r"""Make pasted / Drive-exported thread text easier to read as plain .txt.

    Drives, Gmail, and browsers often insert ``\r``, non-breaking spaces, and
    zero-width characters; long runs of blank lines are common. This does
    *not* change the agent prompt path — it is for curator-facing exports
    (e.g. ``redact_exemplar_pii.py`` output to Google Docs or files).

    When *mark_messages* is true, inserts ``Message N`` banners before
    *From*-shaped lines (Gmail English *On … wrote*, Spanish *día de … de …*,
    *Original Message*). When *elide_print_titles* is true, repeated Gmail
    print / PDF *Correo de…* title lines are collapsed to a single note.
    """
    if not s:
        return ""
    t = s.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\u00a0", " ").replace("\u202f", " ")
    for ch in "\u200b\u200c\u200d\ufeff":
        t = t.replace(ch, "")
    # Thin spaces and similar — normalize to a normal space for readability.
    t = t.replace("\u2009", " ").replace("\u2002", " ").replace("\u2003", " ")
    lines = [line.rstrip() for line in t.split("\n")]
    if elide_print_titles:
        lines = _elide_repeated_gmail_print_headers(lines)
    if mark_messages:
        lines = _join_wrapped_gmail_from_lines(lines)
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t)
    if mark_messages:
        t = mark_message_boundaries_in_export(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip() + "\n"


__all__ = [
    "mark_message_boundaries_in_export",
    "redact_listed_phrases",
    "redact_phrase_map",
    "sanitize",
    "tidy_exemplar_export",
]
