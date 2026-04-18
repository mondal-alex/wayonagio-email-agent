"""Unit tests for kb/artifact.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wayonagio_email_agent.kb import artifact
from wayonagio_email_agent.kb.config import KBConfig


def _cfg(tmp_path: Path, *, gcs_uri: str = "") -> KBConfig:
    return KBConfig(
        enabled=True,
        rag_folder_ids=("r",),
        rag_recursive=True,
        include_mime_types=("text/markdown",),
        embedding_model="x/y",
        gcs_uri=gcs_uri,
        local_dir=str(tmp_path),
        top_k=4,
    )


class TestParseGCSURI:
    def test_parses_bucket_only(self):
        assert artifact._parse_gcs_uri("gs://my-bucket") == ("my-bucket", "")

    def test_parses_bucket_and_prefix(self):
        assert artifact._parse_gcs_uri("gs://my-bucket/kb") == ("my-bucket", "kb")

    def test_strips_trailing_slash_on_prefix(self):
        assert artifact._parse_gcs_uri("gs://my-bucket/kb/") == ("my-bucket", "kb")

    def test_nested_prefix(self):
        assert artifact._parse_gcs_uri("gs://my-bucket/envs/prod/kb") == (
            "my-bucket",
            "envs/prod/kb",
        )

    def test_rejects_non_gs_scheme(self):
        with pytest.raises(ValueError, match="gs://"):
            artifact._parse_gcs_uri("s3://bucket/prefix")

    def test_rejects_missing_bucket(self):
        with pytest.raises(ValueError, match="gs://"):
            artifact._parse_gcs_uri("gs://")


class TestGCSObjectName:
    def test_no_prefix(self):
        assert artifact._gcs_object_name("", "kb.sqlite") == "kb.sqlite"

    def test_with_prefix(self):
        assert artifact._gcs_object_name("env/prod", "kb.sqlite") == "env/prod/kb.sqlite"


class TestLocalRoundTrip:
    def test_upload_and_download_local(self, tmp_path):
        source = tmp_path / "source.txt"
        source.write_text("hello")
        cfg = _cfg(tmp_path / "artifacts")

        destination = artifact.upload_artifact(cfg, source, "kb_index.sqlite")
        assert Path(destination).exists()
        assert Path(destination).read_text() == "hello"

        cache_dir = tmp_path / "cache"
        downloaded = artifact.download_artifact(cfg, "kb_index.sqlite", cache_dir)
        assert downloaded is not None
        assert downloaded.read_text() == "hello"

    def test_download_returns_none_when_missing(self, tmp_path):
        cfg = _cfg(tmp_path / "artifacts")
        result = artifact.download_artifact(cfg, "missing.bin", tmp_path / "cache")
        assert result is None


class TestGCSRoundTripMocked:
    """Verify the GCS code path without a real GCS client.

    We patch ``google.cloud.storage.Client`` at the attribute location where
    ``artifact.py`` imports it (inside the function) to keep the import local.
    """

    def test_upload_calls_storage_client(self, tmp_path, monkeypatch):
        source = tmp_path / "source.bin"
        source.write_bytes(b"payload")

        fake_blob = MagicMock()
        fake_bucket = MagicMock()
        fake_bucket.blob.return_value = fake_blob
        fake_client_cls = MagicMock()
        fake_client_cls.return_value.bucket.return_value = fake_bucket

        class _FakeStorage:
            Client = fake_client_cls

        import sys
        monkeypatch.setitem(sys.modules, "google.cloud.storage", _FakeStorage)

        cfg = _cfg(tmp_path, gcs_uri="gs://my-bucket/kb")
        destination = artifact.upload_artifact(cfg, source, "kb_index.sqlite")

        assert destination == "gs://my-bucket/kb/kb_index.sqlite"
        fake_client_cls.return_value.bucket.assert_called_once_with("my-bucket")
        fake_bucket.blob.assert_called_once_with("kb/kb_index.sqlite")
        fake_blob.upload_from_filename.assert_called_once_with(str(source))

    def test_download_returns_none_when_blob_missing(self, tmp_path, monkeypatch):
        fake_blob = MagicMock()
        fake_blob.exists.return_value = False
        fake_bucket = MagicMock()
        fake_bucket.blob.return_value = fake_blob
        fake_client_cls = MagicMock()
        fake_client_cls.return_value.bucket.return_value = fake_bucket

        class _FakeStorage:
            Client = fake_client_cls

        import sys
        monkeypatch.setitem(sys.modules, "google.cloud.storage", _FakeStorage)

        cfg = _cfg(tmp_path, gcs_uri="gs://my-bucket")
        result = artifact.download_artifact(cfg, "missing", tmp_path / "cache")
        assert result is None
        fake_blob.download_to_filename.assert_not_called()

    def test_download_swallows_gcs_errors(self, tmp_path, monkeypatch, caplog):
        fake_client_cls = MagicMock(side_effect=RuntimeError("credentials missing"))

        class _FakeStorage:
            Client = fake_client_cls

        import sys
        monkeypatch.setitem(sys.modules, "google.cloud.storage", _FakeStorage)

        cfg = _cfg(tmp_path, gcs_uri="gs://my-bucket")
        caplog.set_level("ERROR")
        result = artifact.download_artifact(cfg, "anything", tmp_path / "cache")
        assert result is None
        assert any("Failed to download" in r.message for r in caplog.records)
