"""Format the EXAMPLE RESPONSES prompt block.

Mirrors :func:`kb.retrieve.format_reference_block` for the exemplars side of
the prompt: same delimiter shape (``--- EXAMPLE RESPONSES ---`` /
``--- END EXAMPLE RESPONSES ---``), same numbered ordering, same "empty
input → empty string" behavior so the LLM client can unconditionally
concatenate the result.

The framing text is the most load-bearing thing in this file. It tells the
model two things explicitly:

1. **Style only.** Examples set tone, voice, and structure — not facts.
   Without this hint, the model can copy a real-looking price out of a
   stale exemplar instead of grounding it in the (authoritative) KB
   reference block.
2. **REFERENCE MATERIAL wins.** When an exemplar contradicts the KB —
   prices, durations, inclusions — the KB is canonical. This is the
   single most important instruction in the whole prompt because it
   prevents the most dangerous failure mode (a confidently-wrong reply
   that staff send unmodified).
"""

from __future__ import annotations

from collections.abc import Iterable

from wayonagio_email_agent.exemplars.source import Exemplar

_HEADER = (
    "EXAMPLE RESPONSES (style and tone only): The replies below are "
    "examples of how our team writes. Mirror their voice and structure. "
    "The REFERENCE MATERIAL above is authoritative for facts (prices, "
    "durations, inclusions); if an example contradicts it, follow the "
    "REFERENCE MATERIAL. Do not copy example wording verbatim."
)


def format_exemplar_block(exemplars: Iterable[Exemplar]) -> str:
    """Format *exemplars* as a delimited prompt block, or ``""`` when empty.

    Returning ``""`` for the empty case (instead of an empty
    ``--- EXAMPLE RESPONSES ---`` shell) keeps the day-1 prompt minimal —
    the LLM never sees an empty section header it would have to ignore.

    The numbering ("Example 1", "Example 2", …) is 1-based for human
    readability of the prompt during debugging; the model treats them
    interchangeably either way.
    """
    items = list(exemplars)
    if not items:
        return ""

    parts = [_HEADER, "--- EXAMPLE RESPONSES ---"]
    for index, exemplar in enumerate(items, start=1):
        parts.append(f"Example {index} — {exemplar.title}\n{exemplar.text}")
    parts.append("--- END EXAMPLE RESPONSES ---")
    return "\n\n".join(parts)


__all__ = ["format_exemplar_block"]
