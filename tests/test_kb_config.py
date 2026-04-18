"""Unit tests for kb/config.py."""

from __future__ import annotations

from wayonagio_email_agent.kb import config


class TestParseFolderId:
    def test_accepts_plain_id(self):
        assert config.parse_folder_id("1abcXYZ_-") == "1abcXYZ_-"

    def test_extracts_id_from_share_url(self):
        url = "https://drive.google.com/drive/folders/1abcXYZ_-?usp=sharing"
        assert config.parse_folder_id(url) == "1abcXYZ_-"

    def test_extracts_id_from_open_url(self):
        url = "https://drive.google.com/open?id=1abcXYZ_-"
        assert config.parse_folder_id(url) == "1abcXYZ_-"

    def test_empty_value_returns_empty_string(self):
        assert config.parse_folder_id("") == ""
        assert config.parse_folder_id("   ") == ""


class TestLoad:
    def test_defaults_when_nothing_set(self, monkeypatch):
        for var in (
            "KB_ENABLED",
            "KB_RAG_FOLDER_IDS",
            "KB_RAG_RECURSIVE",
            "KB_INCLUDE_MIME_TYPES",
            "KB_EMBEDDING_MODEL",
            "KB_GCS_URI",
            "KB_LOCAL_DIR",
            "KB_TOP_K",
        ):
            monkeypatch.delenv(var, raising=False)

        cfg = config.load()

        assert cfg.enabled is False
        assert cfg.rag_folder_ids == ()
        assert cfg.rag_recursive is True
        assert cfg.include_mime_types == config.DEFAULT_INCLUDE_MIME_TYPES
        assert cfg.embedding_model == "gemini/text-embedding-004"
        assert cfg.gcs_uri == ""
        assert cfg.local_dir == "./kb_artifacts"
        assert cfg.top_k == 4

    def test_parses_comma_list_of_ids_and_urls(self, monkeypatch):
        monkeypatch.setenv("KB_ENABLED", "true")
        monkeypatch.setenv(
            "KB_RAG_FOLDER_IDS",
            "plainid1, https://drive.google.com/drive/folders/plainid2",
        )

        cfg = config.load()

        assert cfg.enabled is True
        assert cfg.rag_folder_ids == ("plainid1", "plainid2")

    def test_top_k_falls_back_to_default_on_garbage(self, monkeypatch):
        monkeypatch.setenv("KB_TOP_K", "not-a-number")
        assert config.load().top_k == 4

    def test_top_k_clamped_to_at_least_one(self, monkeypatch):
        monkeypatch.setenv("KB_TOP_K", "0")
        assert config.load().top_k == 1
