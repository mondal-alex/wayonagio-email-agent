"""Text extraction for KB sources.

One entry point :func:`extract_text` that dispatches on MIME type to the right
extractor. Kept deliberately small so a future format (HTML, DOCX) is one new
branch, not an architecture change.
"""

from __future__ import annotations

import io
import logging

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from wayonagio_email_agent.kb.drive import DriveFile

logger = logging.getLogger(__name__)

_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_PDF_MIME = "application/pdf"
_TEXT_MIMES = {"text/plain", "text/markdown"}


class ExtractionError(RuntimeError):
    """Raised when text cannot be extracted from a source."""


def extract_text(drive_file: DriveFile, payload: bytes | str) -> str:
    """Extract plain-text content from *payload* based on *drive_file* MIME.

    Returns the extracted text stripped of leading/trailing whitespace. Raises
    :class:`ExtractionError` when the file can't be parsed — the caller is
    expected to skip that file and continue ingest rather than abort.
    """
    mime = drive_file.mime_type

    if mime == _GOOGLE_DOC_MIME:
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace").strip()
        return payload.strip()

    if mime == _PDF_MIME:
        if not isinstance(payload, bytes):
            raise ExtractionError(
                f"PDF payload must be bytes, got {type(payload).__name__}."
            )
        return _extract_pdf(payload, drive_file.path)

    if mime in _TEXT_MIMES:
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace").strip()
        return payload.strip()

    raise ExtractionError(f"Unsupported MIME type: {mime}")


def _extract_pdf(data: bytes, path_for_logging: str) -> str:
    """Extract text from a PDF byte stream.

    We tolerate per-page failures: one broken page should not lose the whole
    document, but a totally unreadable PDF is raised as :class:`ExtractionError`
    so the ingest pipeline can log it and move on.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise ExtractionError(f"Could not open PDF: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - pypdf surfaces many exception types
        # Encrypted / malformed PDFs can raise NotImplementedError, UnicodeError,
        # etc. We treat every open failure the same way: skip this source.
        raise ExtractionError(f"Could not open PDF ({type(exc).__name__}): {exc}") from exc

    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - pypdf raises a variety of things
            logger.warning(
                "PDF %s: page %d failed to extract: %s", path_for_logging, index, exc
            )
            continue
        if text.strip():
            pages.append(text)

    result = "\n\n".join(pages).strip()
    if not result:
        raise ExtractionError("PDF yielded no text (scanned image?).")
    return result
