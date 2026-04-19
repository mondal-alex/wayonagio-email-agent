"""Google Drive API wrapper for KB ingest.

Thin layer over ``google-api-python-client`` that exposes just the operations
the KB needs: list a folder (optionally recursive), export a Google Doc as
plain text, and download a binary file (PDFs, etc).

Credentials reuse the existing Gmail OAuth token — we added
``drive.readonly`` to the scope list in :mod:`gmail_client` so one
authentication flow covers both services.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from wayonagio_email_agent.gmail_client import load_credentials

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


@dataclass(frozen=True)
class DriveFile:
    """A Drive file located during folder traversal.

    ``path`` is the human-readable Drive path we show in retrieval citations
    (e.g. ``"Wayonagio Ops / Tour PDFs 2026 / Machu Picchu Standard.pdf"``).
    """

    id: str
    name: str
    mime_type: str
    path: str
    modified_time: str


def _build_service() -> Any:
    creds = load_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def build_drive_service() -> Any:
    """Public alias for :func:`_build_service`.

    Exists so peer subsystems (notably ``exemplars.source``) can construct a
    single Drive client once and reuse it across a fan-out of parallel reads
    instead of re-authenticating per call. Keeping ``_build_service`` itself
    private preserves the existing convention that internal helpers are
    underscore-prefixed.
    """
    return _build_service()


def list_folder(
    folder_id: str,
    *,
    recursive: bool = True,
    include_mime_types: tuple[str, ...] | None = None,
    service: Any | None = None,
) -> list[DriveFile]:
    """Return every file under *folder_id* whose MIME type is whitelisted.

    When *recursive* is ``True`` (the default) we walk subfolders too. Folder
    entries themselves are never returned — only leaf files. Returned paths
    are rooted at the top-level folder's display name, so downstream
    retrieval citations read like ``"Tours 2026 / Machu Picchu.pdf"``.
    """
    svc = service or _build_service()
    root_name = _get_folder_name(svc, folder_id)
    return _walk(svc, folder_id, root_name, recursive, include_mime_types)


def _walk(
    service: Any,
    folder_id: str,
    base_path: str,
    recursive: bool,
    include_mime_types: tuple[str, ...] | None,
) -> list[DriveFile]:
    files: list[DriveFile] = []
    page_token: str | None = None
    while True:
        try:
            response = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Drive API error listing folder %s: %s", folder_id, exc)
            raise

        for entry in response.get("files", []):
            mime = entry["mimeType"]
            path = f"{base_path} / {entry['name']}"
            if mime == _FOLDER_MIME:
                if recursive:
                    # Subfolder name is already in `entry`, so we don't need
                    # another files().get() round-trip.
                    files.extend(
                        _walk(
                            service,
                            entry["id"],
                            path,
                            recursive=True,
                            include_mime_types=include_mime_types,
                        )
                    )
                continue

            if include_mime_types and mime not in include_mime_types:
                logger.debug("Skipping %s (mime=%s not in allowlist).", path, mime)
                continue

            files.append(
                DriveFile(
                    id=entry["id"],
                    name=entry["name"],
                    mime_type=mime,
                    path=path,
                    modified_time=entry.get("modifiedTime", ""),
                )
            )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def _get_folder_name(service: Any, folder_id: str) -> str:
    try:
        meta = (
            service.files()
            .get(fileId=folder_id, fields="name", supportsAllDrives=True)
            .execute()
        )
    except HttpError as exc:
        logger.warning("Could not fetch folder name for %s: %s", folder_id, exc)
        return folder_id
    return meta.get("name", folder_id)


def export_doc_as_text(file_id: str, *, service: Any | None = None) -> str:
    """Export a Google Doc as plain text."""
    svc = service or _build_service()
    try:
        data = (
            svc.files()
            .export(fileId=file_id, mimeType="text/plain")
            .execute()
        )
    except HttpError as exc:
        logger.error("Drive API error exporting Doc %s: %s", file_id, exc)
        raise
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def download_file(file_id: str, *, service: Any | None = None) -> bytes:
    """Download a binary file (PDF, etc.) as raw bytes."""
    svc = service or _build_service()
    try:
        request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()
    except HttpError as exc:
        logger.error("Drive API error downloading %s: %s", file_id, exc)
        raise


def read_file(drive_file: DriveFile, *, service: Any | None = None) -> bytes | str:
    """Return the file contents in the format its extractor needs.

    Google Docs come back as UTF-8 text (we hit the Drive export endpoint);
    every other MIME returns raw bytes.
    """
    if drive_file.mime_type == _GOOGLE_DOC_MIME:
        return export_doc_as_text(drive_file.id, service=service)
    return download_file(drive_file.id, service=service)
