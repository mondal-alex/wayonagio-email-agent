"""Unit tests for exemplars/prompt.py."""

from __future__ import annotations

from wayonagio_email_agent.exemplars.prompt import format_exemplar_block
from wayonagio_email_agent.exemplars.source import Exemplar


class TestEmpty:
    def test_empty_iterable_returns_empty_string(self):
        assert format_exemplar_block([]) == ""

    def test_iterator_consumed_safely(self):
        """The function must accept any iterable, including a one-shot
        iterator. Returning ``""`` proves we materialized the iterator
        before the truthiness check (otherwise we'd leak a generator)."""
        assert format_exemplar_block(iter([])) == ""


class TestSingleExemplar:
    def test_renders_with_header_and_delimiters(self):
        block = format_exemplar_block(
            [Exemplar(title="Refund policy", text="Hello, thank you...", source_id="x")]
        )

        assert "EXAMPLE RESPONSES (style and tone only)" in block
        assert "REFERENCE MATERIAL above is authoritative" in block
        assert "--- EXAMPLE RESPONSES ---" in block
        assert "--- END EXAMPLE RESPONSES ---" in block
        assert "Example 1 — Refund policy" in block
        assert "Hello, thank you..." in block

    def test_kb_precedence_framing_is_present(self):
        """Anchor the most important instruction in the whole prompt: when
        exemplars and the KB disagree on facts, the KB wins. If this string
        ever drifts, the model can be tricked by a stale exemplar into
        contradicting the canonical agency content."""
        block = format_exemplar_block(
            [Exemplar(title="t", text="b", source_id="x")]
        )
        assert "if an example contradicts it, follow the REFERENCE MATERIAL" in block
        assert "Do not copy example wording verbatim" in block


class TestMultipleExemplars:
    def test_numbered_in_order(self):
        block = format_exemplar_block(
            [
                Exemplar(title="First", text="A", source_id="1"),
                Exemplar(title="Second", text="B", source_id="2"),
                Exemplar(title="Third", text="C", source_id="3"),
            ]
        )
        assert "Example 1 — First" in block
        assert "Example 2 — Second" in block
        assert "Example 3 — Third" in block

        # And in the right order on the page.
        first = block.index("Example 1 — First")
        second = block.index("Example 2 — Second")
        third = block.index("Example 3 — Third")
        assert first < second < third

    def test_blocks_separated_by_blank_lines(self):
        """Two consecutive newlines is the LLM-friendly delimiter — the
        model attends to paragraph boundaries."""
        block = format_exemplar_block(
            [
                Exemplar(title="A", text="body A", source_id="1"),
                Exemplar(title="B", text="body B", source_id="2"),
            ]
        )
        assert "body A\n\nExample 2 — B" in block
