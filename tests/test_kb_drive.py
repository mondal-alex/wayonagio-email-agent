"""Unit tests for kb/drive.py.

We stub the Google API discovery client with a fake that mirrors the small
subset of behavior we use: `files().list()` with pagination,
`files().get()`, `files().export()` and `files().get_media()`. This keeps
the tests hermetic — no real Drive credentials required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wayonagio_email_agent.kb import drive


class _Executable:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeFiles:
    def __init__(self, folder_meta, folder_contents, export_bytes, media_bytes):
        self._folder_meta = folder_meta
        self._folder_contents = folder_contents
        self._export_bytes = export_bytes
        self._media_bytes = media_bytes

    def list(self, *, q, **_kwargs):
        folder_id = q.split("'")[1]
        contents = self._folder_contents.get(folder_id, [])
        return _Executable({"files": contents, "nextPageToken": None})

    def get(self, *, fileId, **_kwargs):
        return _Executable({"name": self._folder_meta.get(fileId, fileId)})

    def export(self, *, fileId, mimeType):
        return _Executable(self._export_bytes[fileId])

    def get_media(self, *, fileId, **_kwargs):
        self._last_media = (fileId, self._media_bytes[fileId])
        return MagicMock()


class _FakeService:
    def __init__(self, fake_files: _FakeFiles):
        self._files = fake_files

    def files(self):
        return self._files


def test_list_folder_filters_by_mime_and_recurses():
    folder_meta = {"root": "RootFolder", "sub": "Subfolder"}
    contents = {
        "root": [
            {
                "id": "doc1",
                "name": "Welcome.gdoc",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "t1",
            },
            {
                "id": "pdf1",
                "name": "Tour.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "t2",
            },
            {
                "id": "img",
                "name": "logo.png",
                "mimeType": "image/png",
                "modifiedTime": "t3",
            },
            {
                "id": "sub",
                "name": "Subfolder",
                "mimeType": "application/vnd.google-apps.folder",
                "modifiedTime": "t4",
            },
        ],
        "sub": [
            {
                "id": "txt1",
                "name": "notes.md",
                "mimeType": "text/markdown",
                "modifiedTime": "t5",
            },
        ],
    }

    svc = _FakeService(_FakeFiles(folder_meta, contents, {}, {}))

    files = drive.list_folder(
        "root",
        recursive=True,
        include_mime_types=(
            "application/pdf",
            "application/vnd.google-apps.document",
            "text/markdown",
        ),
        service=svc,
    )

    paths = [f.path for f in files]
    names = [f.name for f in files]
    assert "logo.png" not in names, "image/png should be filtered out"
    assert "Welcome.gdoc" in names
    assert "Tour.pdf" in names
    assert "notes.md" in names
    nested = next(f for f in files if f.name == "notes.md")
    assert nested.path == "RootFolder / Subfolder / notes.md"
    assert all(p.startswith("RootFolder") for p in paths)


def test_list_folder_without_recursion_ignores_subfolders():
    folder_meta = {"root": "Root", "sub": "Sub"}
    contents = {
        "root": [
            {
                "id": "sub",
                "name": "Sub",
                "mimeType": "application/vnd.google-apps.folder",
                "modifiedTime": "t",
            },
        ],
        "sub": [
            {
                "id": "txt1",
                "name": "notes.md",
                "mimeType": "text/markdown",
                "modifiedTime": "t",
            },
        ],
    }
    svc = _FakeService(_FakeFiles(folder_meta, contents, {}, {}))

    files = drive.list_folder("root", recursive=False, service=svc)
    assert files == []


class _PaginatedFakeFiles:
    """Drive .list() returning multiple pages via nextPageToken.

    The default `_FakeFiles` always returns ``nextPageToken=None`` so the
    pagination loop in `_walk` runs exactly once. Real Drive responses cap
    out at pageSize=1000 entries; we need an explicit regression test that
    the loop honours pageToken for folders larger than that.
    """

    def __init__(self, pages_by_folder, folder_meta):
        self._pages = pages_by_folder
        self._folder_meta = folder_meta
        self.list_calls: list[dict] = []

    def list(self, *, q, pageToken=None, **_kwargs):
        folder_id = q.split("'")[1]
        self.list_calls.append({"folder_id": folder_id, "pageToken": pageToken})
        pages = self._pages.get(folder_id, [])
        page_idx = 0 if pageToken is None else int(pageToken)
        files = pages[page_idx] if page_idx < len(pages) else []
        next_token = (
            str(page_idx + 1) if page_idx + 1 < len(pages) else None
        )
        return _Executable({"files": files, "nextPageToken": next_token})

    def get(self, *, fileId, **_kwargs):
        return _Executable({"name": self._folder_meta.get(fileId, fileId)})


def test_list_folder_paginates_with_next_page_token():
    """Folders with >pageSize entries must come back complete: every page is
    requested and the union of files is returned.
    """
    pages = {
        "root": [
            [
                {
                    "id": "p1f1",
                    "name": "page1-doc.md",
                    "mimeType": "text/markdown",
                    "modifiedTime": "t1",
                },
                {
                    "id": "p1f2",
                    "name": "page1-tour.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "t2",
                },
            ],
            [
                {
                    "id": "p2f1",
                    "name": "page2-doc.md",
                    "mimeType": "text/markdown",
                    "modifiedTime": "t3",
                },
            ],
            [
                {
                    "id": "p3f1",
                    "name": "page3-doc.md",
                    "mimeType": "text/markdown",
                    "modifiedTime": "t4",
                },
            ],
        ]
    }
    fake_files = _PaginatedFakeFiles(pages, {"root": "Root"})
    svc = _FakeService(fake_files)

    files = drive.list_folder(
        "root",
        recursive=False,
        include_mime_types=("text/markdown", "application/pdf"),
        service=svc,
    )

    names = sorted(f.name for f in files)
    assert names == [
        "page1-doc.md",
        "page1-tour.pdf",
        "page2-doc.md",
        "page3-doc.md",
    ]
    # Anchors that we issued exactly one call per page and threaded the token
    # back in. If pagination breaks, we'd see only a single call.
    page_tokens = [c["pageToken"] for c in fake_files.list_calls]
    assert page_tokens == [None, "1", "2"]


def test_export_doc_as_text_returns_decoded_string():
    svc = _FakeService(_FakeFiles({}, {}, {"doc1": b"hello \xe2\x98\x83"}, {}))
    assert drive.export_doc_as_text("doc1", service=svc) == "hello ☃"


def test_read_file_dispatches_on_mime(monkeypatch):
    svc = _FakeService(
        _FakeFiles({}, {}, {"doc1": b"text"}, {"pdf1": b"%PDF-..."})
    )

    doc = drive.DriveFile(
        id="doc1",
        name="doc",
        mime_type="application/vnd.google-apps.document",
        path="root / doc",
        modified_time="t",
    )
    assert drive.read_file(doc, service=svc) == "text"

    pdf = drive.DriveFile(
        id="pdf1",
        name="tour.pdf",
        mime_type="application/pdf",
        path="root / tour.pdf",
        modified_time="t",
    )

    captured = {}

    class _FakeDownloader:
        def __init__(self, buf, _req):
            self._buf = buf

        def next_chunk(self):
            self._buf.write(b"%PDF-...")
            return (None, True)

    monkeypatch.setattr(drive, "MediaIoBaseDownload", _FakeDownloader)
    result = drive.read_file(pdf, service=svc)
    assert result == b"%PDF-..."
