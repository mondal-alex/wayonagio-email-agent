"""Unit tests for exemplars/loader.py.

The loader's contract is "never raise, always cache". These tests pin all
three failure shapes (disabled config, source raises, source returns []) to
the same observable behavior — an empty list and a process-lifetime cache.
"""

from __future__ import annotations

import logging

import pytest

from wayonagio_email_agent.exemplars import loader
from wayonagio_email_agent.exemplars.config import ExemplarConfig
from wayonagio_email_agent.exemplars.source import Exemplar


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with an empty cache so state doesn't bleed."""
    loader.reset()
    yield
    loader.reset()


def _enabled_cfg() -> ExemplarConfig:
    return ExemplarConfig(folder_ids=("f1",), include_mime_types=())


def _disabled_cfg() -> ExemplarConfig:
    return ExemplarConfig(folder_ids=(), include_mime_types=())


class TestColdStartLoad:
    def test_first_call_invokes_source_and_caches(self, monkeypatch):
        calls = []
        sample = [
            Exemplar(title="A", text="body A", source_id="id-a"),
            Exemplar(title="B", text="body B", source_id="id-b"),
        ]

        def _collect(cfg):
            calls.append(cfg)
            return list(sample)

        monkeypatch.setattr(loader.exemplar_config, "load", _enabled_cfg)
        monkeypatch.setattr(loader.exemplar_source, "collect", _collect)

        first = loader.get_all_exemplars()
        second = loader.get_all_exemplars()

        assert first == sample
        assert second == sample
        # Cached after first call — collect must NOT run twice.
        assert len(calls) == 1


class TestDisabledConfig:
    def test_disabled_caches_empty_without_calling_source(self, monkeypatch):
        monkeypatch.setattr(loader.exemplar_config, "load", _disabled_cfg)
        monkeypatch.setattr(
            loader.exemplar_source,
            "collect",
            lambda cfg: pytest.fail("collect should not run when disabled"),
        )

        assert loader.get_all_exemplars() == []
        # Subsequent call uses cache, not config.
        assert loader.get_all_exemplars() == []


class TestFailureCachesEmptyWithoutRaising:
    def test_source_raising_caches_empty_and_logs_warning(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(loader.exemplar_config, "load", _enabled_cfg)

        def _boom(cfg):
            raise RuntimeError("Drive 503")

        monkeypatch.setattr(loader.exemplar_source, "collect", _boom)

        with caplog.at_level(logging.WARNING, logger=loader.__name__):
            result = loader.get_all_exemplars()

        assert result == []
        assert any(
            "Exemplar load failed" in r.getMessage() for r in caplog.records
        )

    def test_failure_is_cached_for_process_lifetime(self, monkeypatch):
        """Failed loads must not be retried per-request — that would risk
        thrashing Drive if it stays down. Cloud Run instance cycling is the
        supported refresh path."""
        monkeypatch.setattr(loader.exemplar_config, "load", _enabled_cfg)

        call_count = 0

        def _boom(cfg):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Drive 503")

        monkeypatch.setattr(loader.exemplar_source, "collect", _boom)

        loader.get_all_exemplars()
        loader.get_all_exemplars()
        loader.get_all_exemplars()

        assert call_count == 1, "loader retried after a cached failure"

    def test_config_load_raising_is_caught(self, monkeypatch):
        """If a future ``config.load`` adds validation that raises, the
        loader must still degrade gracefully — exemplars are optional."""

        def _boom_cfg():
            raise RuntimeError("env var KB_EXEMPLAR_FOLDER_IDS malformed")

        monkeypatch.setattr(loader.exemplar_config, "load", _boom_cfg)

        result = loader.get_all_exemplars()
        assert result == []


class TestReset:
    def test_reset_re_runs_source_on_next_call(self, monkeypatch):
        sample = [Exemplar(title="A", text="x", source_id="id")]
        call_count = 0

        def _collect(cfg):
            nonlocal call_count
            call_count += 1
            return list(sample)

        monkeypatch.setattr(loader.exemplar_config, "load", _enabled_cfg)
        monkeypatch.setattr(loader.exemplar_source, "collect", _collect)

        loader.get_all_exemplars()
        loader.reset()
        loader.get_all_exemplars()

        assert call_count == 2


class TestNeverRaisesInvariant:
    """Guard: every call site (api.py warm-up, llm/client.generate_reply)
    relies on ``get_all_exemplars`` not raising. Anchor that contract here
    so a future refactor that removes the broad ``except`` fails this test
    instead of breaking production drafting."""

    def test_no_exception_propagates_from_get_all_exemplars(self, monkeypatch):
        def _boom_cfg():
            raise RuntimeError("config exploded")

        def _boom_collect(cfg):
            raise RuntimeError("source exploded")

        monkeypatch.setattr(loader.exemplar_config, "load", _boom_cfg)
        monkeypatch.setattr(loader.exemplar_source, "collect", _boom_collect)

        # This must not raise. If it does, the next line is never reached
        # and the test fails with the propagated exception (which is also
        # a clear signal of the regression).
        assert loader.get_all_exemplars() == []
