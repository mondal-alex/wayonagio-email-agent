"""Unit tests for exemplars/config.py."""

from __future__ import annotations

from wayonagio_email_agent.exemplars import config


def _clear_env(monkeypatch):
    for var in (
        "KB_EXEMPLAR_FOLDER_IDS",
        "KB_EXEMPLAR_INCLUDE_MIME_TYPES",
    ):
        monkeypatch.delenv(var, raising=False)


class TestLoad:
    def test_disabled_when_folder_ids_unset(self, monkeypatch):
        _clear_env(monkeypatch)
        cfg = config.load()
        assert cfg.enabled is False
        assert cfg.folder_ids == ()

    def test_disabled_when_folder_ids_blank(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KB_EXEMPLAR_FOLDER_IDS", "   ")
        cfg = config.load()
        assert cfg.enabled is False
        assert cfg.folder_ids == ()

    def test_enabled_when_single_id_set(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KB_EXEMPLAR_FOLDER_IDS", "1abc")
        cfg = config.load()
        assert cfg.enabled is True
        assert cfg.folder_ids == ("1abc",)
        assert cfg.include_mime_types == config.DEFAULT_INCLUDE_MIME_TYPES

    def test_parses_csv_of_ids_and_share_urls(self, monkeypatch):
        """Reuses kb.config.parse_folder_id so curators can paste anything
        the Drive UI gives them — raw IDs, /folders/ URLs, ?id= URLs."""
        _clear_env(monkeypatch)
        monkeypatch.setenv(
            "KB_EXEMPLAR_FOLDER_IDS",
            "plainid1, https://drive.google.com/drive/folders/plainid2",
        )
        cfg = config.load()
        assert cfg.folder_ids == ("plainid1", "plainid2")

    def test_custom_mime_types_override_default(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KB_EXEMPLAR_FOLDER_IDS", "1abc")
        monkeypatch.setenv(
            "KB_EXEMPLAR_INCLUDE_MIME_TYPES", "text/plain, text/markdown"
        )
        cfg = config.load()
        assert cfg.include_mime_types == ("text/plain", "text/markdown")

    def test_blank_mime_env_falls_back_to_default(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KB_EXEMPLAR_FOLDER_IDS", "1abc")
        monkeypatch.setenv("KB_EXEMPLAR_INCLUDE_MIME_TYPES", "   ")
        cfg = config.load()
        assert cfg.include_mime_types == config.DEFAULT_INCLUDE_MIME_TYPES

    def test_enabled_property_is_consistent_with_folder_ids(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KB_EXEMPLAR_FOLDER_IDS", "")
        assert config.load().enabled is False
        monkeypatch.setenv("KB_EXEMPLAR_FOLDER_IDS", "x")
        assert config.load().enabled is True
