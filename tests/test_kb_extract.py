"""Unit tests for kb/extract.py."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from wayonagio_email_agent.kb.drive import DriveFile
from wayonagio_email_agent.kb.extract import ExtractionError, extract_text


def _drive_file(mime: str) -> DriveFile:
    return DriveFile(
        id="id-1",
        name=f"test.{mime.split('/')[-1]}",
        mime_type=mime,
        path=f"root / test.{mime.split('/')[-1]}",
        modified_time="2026-04-17T00:00:00Z",
    )


def _minimal_pdf_with_text() -> bytes:
    """Build a PDF that contains extractable text.

    pypdf's test harness can reliably re-read a PDF that it created page-by-page
    with an embedded content stream. We don't try to match real-world PDF
    complexity here — just "pypdf can open it and extract_text returns non-empty".
    """
    # A trivially small PDF with a visible text object. Generated offline to
    # avoid reaching for reportlab just to satisfy extract tests.
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>endobj\n"
        b"4 0 obj<</Length 55>>stream\n"
        b"BT /F1 24 Tf 72 700 Td (Hello Machu Picchu) Tj ET\n"
        b"endstream endobj\n"
        b"xref\n0 5\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000052 00000 n \n"
        b"0000000095 00000 n \n"
        b"0000000229 00000 n \n"
        b"trailer<</Size 5/Root 1 0 R>>\n"
        b"startxref\n330\n%%EOF\n"
    )


class TestExtract:
    def test_plain_text(self):
        drive_file = _drive_file("text/plain")
        assert extract_text(drive_file, b"  hello\n") == "hello"

    def test_markdown(self):
        drive_file = _drive_file("text/markdown")
        assert extract_text(drive_file, "# Title\n\nBody").startswith("# Title")

    def test_google_doc_bytes(self):
        drive_file = _drive_file("application/vnd.google-apps.document")
        assert extract_text(drive_file, b"Exported text  ") == "Exported text"

    def test_google_doc_str(self):
        drive_file = _drive_file("application/vnd.google-apps.document")
        assert extract_text(drive_file, "Already text ") == "Already text"

    def test_pdf_requires_bytes(self):
        drive_file = _drive_file("application/pdf")
        with pytest.raises(ExtractionError):
            extract_text(drive_file, "not bytes")  # type: ignore[arg-type]

    def test_unsupported_mime_raises(self):
        drive_file = _drive_file("application/zip")
        with pytest.raises(ExtractionError):
            extract_text(drive_file, b"PK...")

    def test_broken_pdf_raises(self):
        drive_file = _drive_file("application/pdf")
        with pytest.raises(ExtractionError):
            extract_text(drive_file, b"not a pdf at all")

    def test_pdf_with_no_extractable_text_raises(self):
        """Blank pypdf page -> no text -> clean ExtractionError for caller to skip."""
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = io.BytesIO()
        writer.write(buf)

        drive_file = _drive_file("application/pdf")
        with pytest.raises(ExtractionError):
            extract_text(drive_file, buf.getvalue())
