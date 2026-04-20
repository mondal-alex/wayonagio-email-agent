"""Unit tests for kb/drive.py.

We stub the Google API discovery client with a fake that mirrors the small
subset of behavior we use: `files().list()` with pagination,
`files().get()`, `files().export()` and `files().get_media()`. This keeps
the tests hermetic — no real Drive credentials required.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from wayonagio_email_agent.kb import drive


def _http_error(status: int, reason: str) -> HttpError:
    """Build a real :class:`HttpError` — the constructor wants a ``httplib2``-
    shaped response and a bytes body, so we synthesize the minimum the
    internal parsing needs (status code + a JSON-ish body)."""
    resp = type("R", (), {"status": status, "reason": reason})()
    body = (
        f'{{"error": {{"code": {status}, "message": "{reason}"}}}}'
    ).encode()
    return HttpError(resp, body)


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


# ---------------------------------------------------------------------------
# HttpError-path tests
#
# The happy-path tests above cover ~80% of the module. These anchor the
# failure paths: every Drive call is wrapped in a ``try/except HttpError``
# that either re-raises (list / export / download) or degrades gracefully
# (_get_folder_name falls back to the folder ID so ingest can still proceed).
# Without these tests the pagination loop, the exports, the downloads, and
# the folder-name lookup could all silently swallow errors without anyone
# noticing during a review.
# ---------------------------------------------------------------------------


class _RaisingExecute:
    """Stand-in for a Drive request whose ``.execute()`` raises HttpError.

    We use this instead of `_Executable` to exercise the `except HttpError`
    branches in the module under test. Real HttpErrors surface from
    ``.execute()`` (the point where the HTTP round-trip actually happens),
    so raising there matches production behavior.
    """

    def __init__(self, error: HttpError):
        self._error = error

    def execute(self):
        raise self._error


class _ErroringFiles:
    """Fake ``files()`` whose specific methods raise HttpError on execute."""

    def __init__(
        self,
        *,
        list_error: HttpError | None = None,
        get_error: HttpError | None = None,
        export_error: HttpError | None = None,
        get_media_error: HttpError | None = None,
    ):
        self._list_error = list_error
        self._get_error = get_error
        self._export_error = export_error
        self._get_media_error = get_media_error

    def list(self, **_kwargs):
        if self._list_error is not None:
            return _RaisingExecute(self._list_error)
        return _Executable({"files": [], "nextPageToken": None})

    def get(self, **_kwargs):
        if self._get_error is not None:
            return _RaisingExecute(self._get_error)
        return _Executable({"name": "Anything"})

    def export(self, **_kwargs):
        if self._export_error is not None:
            return _RaisingExecute(self._export_error)
        return _Executable(b"text")

    def get_media(self, **_kwargs):
        if self._get_media_error is not None:
            # get_media itself is what raises for 404s — the error can fire
            # at request-build time (unusual but possible for strict client
            # versions) or during the MediaIoBaseDownload chunk loop.
            raise self._get_media_error
        return MagicMock()


class TestListFolderHttpError:
    def test_list_folder_surfaces_http_error(self, caplog):
        """A 403/404 during paging must not be swallowed — ingest failing
        loud here is how the operator learns the service account is
        missing ``drive.readonly`` on the folder."""
        err = _http_error(403, "forbidden")
        svc = _FakeService(
            _ErroringFiles(list_error=err)
        )

        with caplog.at_level(logging.ERROR, logger="wayonagio_email_agent.kb.drive"):
            with pytest.raises(HttpError):
                drive.list_folder("root", recursive=False, service=svc)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Drive API error listing folder" in m and "root" in m
            for m in messages
        ), "expected an ERROR log that names the folder ID"


class TestGetFolderNameHttpError:
    def test_missing_folder_falls_back_to_id_and_logs_warning(self, caplog):
        """``_get_folder_name`` is best-effort: if it can't resolve the
        folder's display name, the Drive-path prefix in retrieval
        citations becomes the raw folder ID instead of a human name.
        That's ugly but not fatal, so we degrade gracefully with a
        WARNING rather than aborting the whole ingest."""
        err = _http_error(404, "not found")
        svc = _FakeService(_ErroringFiles(get_error=err))

        with caplog.at_level(logging.WARNING, logger="wayonagio_email_agent.kb.drive"):
            # Calling _get_folder_name directly keeps the assertion tight.
            # It's a module-private helper, but we test it to pin the
            # graceful-degradation contract — the fall-back is what keeps
            # a partial Drive misconfig (valid ingest folder, missing .get
            # permission) from taking down the whole ingest run.
            name = drive._get_folder_name(svc, "fid-xyz")

        assert name == "fid-xyz"
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Could not fetch folder name" in m and "fid-xyz" in m
            for m in messages
        )


class TestExportDocAsTextHttpError:
    def test_export_surfaces_http_error(self, caplog):
        err = _http_error(500, "backend error")
        svc = _FakeService(_ErroringFiles(export_error=err))

        with caplog.at_level(logging.ERROR, logger="wayonagio_email_agent.kb.drive"):
            with pytest.raises(HttpError):
                drive.export_doc_as_text("doc-broken", service=svc)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Drive API error exporting Doc" in m and "doc-broken" in m
            for m in messages
        )


class TestDownloadFileHttpError:
    def test_download_surfaces_http_error(self, caplog):
        """A 404 during ``get_media`` must propagate so ingest can report
        which specific file is broken and skip it at the orchestrator
        level instead of producing a truncated index."""
        err = _http_error(404, "not found")
        svc = _FakeService(_ErroringFiles(get_media_error=err))

        with caplog.at_level(logging.ERROR, logger="wayonagio_email_agent.kb.drive"):
            with pytest.raises(HttpError):
                drive.download_file("pdf-broken", service=svc)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Drive API error downloading" in m and "pdf-broken" in m
            for m in messages
        )
