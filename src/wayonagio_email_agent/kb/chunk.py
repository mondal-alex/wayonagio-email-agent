"""Paragraph-aware chunker.

We keep the chunker simple and deterministic: split on blank-line paragraph
boundaries, then greedily pack paragraphs into chunks until the next paragraph
would exceed the target size. Oversized single paragraphs are broken on
whitespace. Consecutive chunks carry a configurable overlap so a retriever
reading the middle of a paragraph still gets its surrounding sentence.

Token counting uses a *characters / 4* heuristic — roughly the Gemini /
OpenAI ratio for English/Romance languages. Perfect enough for retrieval
sizing and avoids pulling in tiktoken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_CHUNK_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 100
_APPROX_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Chunk:
    """A slice of extracted text, ready to embed."""

    index: int
    text: str
    source_id: str
    source_name: str
    source_path: str


def approximate_token_count(text: str) -> int:
    """Cheap approximation so we don't have to depend on a tokenizer."""
    return max(1, len(text) // _APPROX_CHARS_PER_TOKEN)


def _chunk_chars(target_tokens: int) -> int:
    return max(1, target_tokens * _APPROX_CHARS_PER_TOKEN)


def _split_paragraphs(text: str) -> list[str]:
    normalized = re.sub(r"\r\n?", "\n", text)
    paragraphs = re.split(r"\n\s*\n+", normalized)
    return [p.strip() for p in paragraphs if p.strip()]


def _split_oversized(paragraph: str, max_chars: int) -> list[str]:
    """Split a single oversized paragraph on whitespace boundaries."""
    if len(paragraph) <= max_chars:
        return [paragraph]

    words = paragraph.split()
    out: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        additional = len(word) + (1 if current else 0)
        if current_len + additional > max_chars and current:
            out.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += additional
    if current:
        out.append(" ".join(current))
    return out


def chunk_text(
    text: str,
    *,
    source_id: str,
    source_name: str,
    source_path: str,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Split *text* into :class:`Chunk` objects.

    * Paragraph boundaries are respected where possible.
    * Chunks never exceed ``chunk_tokens`` (approximated).
    * Consecutive chunks share up to ``overlap_tokens`` of trailing text so
      retrieval of a mid-paragraph passage still has surrounding context.
    """
    if not text.strip():
        return []

    max_chars = _chunk_chars(chunk_tokens)
    overlap_chars = min(
        _chunk_chars(overlap_tokens) if overlap_tokens > 0 else 0, max_chars - 1
    )

    raw_paragraphs = _split_paragraphs(text)
    paragraphs: list[str] = []
    for paragraph in raw_paragraphs:
        paragraphs.extend(_split_oversized(paragraph, max_chars))

    chunks: list[Chunk] = []
    buffer = ""
    for paragraph in paragraphs:
        if not buffer:
            buffer = paragraph
            continue

        candidate = f"{buffer}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            buffer = candidate
            continue

        chunks.append(_make_chunk(buffer, len(chunks), source_id, source_name, source_path))
        tail = buffer[-overlap_chars:] if overlap_chars else ""
        buffer = f"{tail}\n\n{paragraph}" if tail else paragraph

    if buffer:
        chunks.append(_make_chunk(buffer, len(chunks), source_id, source_name, source_path))

    return chunks


def _make_chunk(
    text: str,
    index: int,
    source_id: str,
    source_name: str,
    source_path: str,
) -> Chunk:
    return Chunk(
        index=index,
        text=text.strip(),
        source_id=source_id,
        source_name=source_name,
        source_path=source_path,
    )
