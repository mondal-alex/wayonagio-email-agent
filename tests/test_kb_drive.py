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
