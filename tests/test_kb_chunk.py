"""Unit tests for kb/chunk.py."""

from __future__ import annotations

from wayonagio_email_agent.kb.chunk import chunk_text


def _all_text(chunks):
    return "\n\n".join(c.text for c in chunks)


def test_empty_input_returns_no_chunks():
    assert chunk_text("", source_id="a", source_name="a", source_path="a") == []
    assert chunk_text("   \n\n   ", source_id="a", source_name="a", source_path="a") == []


def test_short_text_stays_in_one_chunk():
    text = "Hello from Cusco.\n\nBook with us."
    chunks = chunk_text(text, source_id="s", source_name="n", source_path="p")
    assert len(chunks) == 1
    assert "Hello from Cusco." in chunks[0].text
    assert chunks[0].source_id == "s"
    assert chunks[0].source_name == "n"
    assert chunks[0].source_path == "p"
    assert chunks[0].index == 0


def test_splits_on_paragraph_boundary_when_exceeding_target():
    paragraph = ("A" * 400 + "\n\n") * 4
    chunks = chunk_text(
        paragraph.strip(),
        source_id="s",
        source_name="n",
        source_path="p",
        chunk_tokens=200,
        overlap_tokens=0,
    )
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.text) <= 200 * 4


def test_consecutive_chunks_share_overlap():
    paragraphs = "\n\n".join(f"Paragraph {i} " + ("text " * 80) for i in range(6))
    chunks = chunk_text(
        paragraphs,
        source_id="s",
        source_name="n",
        source_path="p",
        chunk_tokens=200,
        overlap_tokens=40,
    )
    assert len(chunks) >= 2
    first_tail = chunks[0].text[-120:]
    assert any(
        word in chunks[1].text for word in first_tail.split() if len(word) > 3
    ), "Overlap text should appear in the next chunk."


def test_oversized_single_paragraph_is_split_on_word_boundaries():
    paragraph = ("word " * 2000).strip()
    chunks = chunk_text(
        paragraph,
        source_id="s",
        source_name="n",
        source_path="p",
        chunk_tokens=200,
        overlap_tokens=0,
    )
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c.text) <= 200 * 4
        assert "word" in c.text


def test_preserves_source_metadata_across_all_chunks():
    text = "\n\n".join(["para " + "x" * 500 for _ in range(10)])
    chunks = chunk_text(
        text,
        source_id="drive-id-1",
        source_name="Machu.pdf",
        source_path="root / Machu.pdf",
        chunk_tokens=200,
    )
    assert len(chunks) > 1
    for i, c in enumerate(chunks):
        assert c.source_id == "drive-id-1"
        assert c.source_name == "Machu.pdf"
        assert c.source_path == "root / Machu.pdf"
        assert c.index == i
