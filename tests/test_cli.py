"""Unit tests for cli.py."""

from __future__ import annotations

from unittest.mock import patch

import click
from click.testing import CliRunner

from wayonagio_email_agent.cli import cli


class TestScanCommand:
    def test_scan_refuses_to_start_when_feature_flag_disabled(self):
        runner = CliRunner()

        with patch("wayonagio_email_agent.agent.scanner_enabled", return_value=False):
            result = runner.invoke(cli, ["scan"])

        assert result.exit_code != 0
        assert "SCANNER_ENABLED=true" in result.output

    def test_scan_starts_when_feature_flag_enabled(self):
        runner = CliRunner()

        with (
            patch("wayonagio_email_agent.agent.scanner_enabled", return_value=True),
            patch("wayonagio_email_agent.agent.scan_loop") as mock_scan_loop,
        ):
            result = runner.invoke(cli, ["scan", "--interval", "60", "--dry-run"])

        assert result.exit_code == 0
        mock_scan_loop.assert_called_once_with(interval=60, dry_run=True)

    def test_scan_uses_default_interval_when_flag_not_provided(self):
        runner = CliRunner()

        with (
            patch("wayonagio_email_agent.agent.scanner_enabled", return_value=True),
            patch("wayonagio_email_agent.agent.scan_loop") as mock_scan_loop,
        ):
            result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 0
        mock_scan_loop.assert_called_once_with(interval=1800, dry_run=False)


class TestScanOnceCommand:
    """``scan-once`` is the one-shot entry point for external schedulers
    (Cloud Run Jobs + Cloud Scheduler, cron, etc.). Unlike ``scan`` it must
    not loop — it runs a single pass and exits so the scheduler can own the
    cadence and the process lifecycle.
    """

    def test_refuses_to_run_when_feature_flag_disabled(self):
        runner = CliRunner()

        with patch("wayonagio_email_agent.agent.scanner_enabled", return_value=False):
            result = runner.invoke(cli, ["scan-once"])

        assert result.exit_code != 0
        assert "SCANNER_ENABLED=true" in result.output

    def test_runs_single_pass_when_enabled(self):
        runner = CliRunner()

        with (
            patch("wayonagio_email_agent.agent.scanner_enabled", return_value=True),
            patch("wayonagio_email_agent.agent.scan_once") as mock_scan_once,
            patch("wayonagio_email_agent.agent.scan_loop") as mock_scan_loop,
        ):
            result = runner.invoke(cli, ["scan-once"])

        assert result.exit_code == 0
        mock_scan_once.assert_called_once_with(dry_run=False)
        mock_scan_loop.assert_not_called()

    def test_forwards_dry_run_flag(self):
        runner = CliRunner()

        with (
            patch("wayonagio_email_agent.agent.scanner_enabled", return_value=True),
            patch("wayonagio_email_agent.agent.scan_once") as mock_scan_once,
        ):
            result = runner.invoke(cli, ["scan-once", "--dry-run"])

        assert result.exit_code == 0
        mock_scan_once.assert_called_once_with(dry_run=True)


class TestKBIngestCommand:
    def test_invokes_ingest_run(self):
        runner = CliRunner()

        fake_result = type(
            "R",
            (),
            dict(
                rag_source_count=3,
                rag_chunk_count=14,
                embedding_dim=768,
                index_destination="/tmp/kb_index.sqlite",
            ),
        )()

        with patch("wayonagio_email_agent.kb.ingest.run", return_value=fake_result) as mock_run:
            result = runner.invoke(cli, ["kb-ingest"])

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once_with()
        assert "rag_sources=3" in result.output
        assert "/tmp/kb_index.sqlite" in result.output


class TestDraftReplyCommand:
    def test_prints_draft_id_on_success(self):
        runner = CliRunner()
        with patch(
            "wayonagio_email_agent.agent.manual_draft_flow",
            return_value={"id": "draft-123"},
        ):
            result = runner.invoke(cli, ["draft-reply", "msg-1"])
        assert result.exit_code == 0, result.output
        assert "draft-123" in result.output

    def test_kb_unavailable_surfaces_clean_error(self):
        """Same contract as the API's 503: operators must see an actionable
        one-line error pointing at kb-ingest, not a Python traceback."""
        runner = CliRunner()

        from wayonagio_email_agent.kb.retrieve import KBUnavailableError

        with patch(
            "wayonagio_email_agent.agent.manual_draft_flow",
            side_effect=KBUnavailableError(
                "KB index artifact could not be downloaded."
            ),
        ):
            result = runner.invoke(cli, ["draft-reply", "msg-1"])

        assert result.exit_code != 0
        assert "kb-ingest" in result.output
        assert "Traceback" not in result.output

    def test_empty_reply_surfaces_clean_error(self):
        runner = CliRunner()

        from wayonagio_email_agent.llm.client import EmptyReplyError

        with patch(
            "wayonagio_email_agent.agent.manual_draft_flow",
            side_effect=EmptyReplyError("LLM returned an empty reply."),
        ):
            result = runner.invoke(cli, ["draft-reply", "msg-1"])

        assert result.exit_code != 0
        assert "empty reply" in result.output.lower()
        assert "Traceback" not in result.output


class TestKBSearchCommand:
    def test_prints_results_when_hits_exist(self):
        runner = CliRunner()

        from wayonagio_email_agent.kb.store import ScoredChunk

        hits = [
            ScoredChunk(
                text="Machu Picchu tour details.",
                source_id="sid",
                source_name="a.md",
                source_path="root / a.md",
                chunk_index=0,
                score=0.87,
            )
        ]
        with patch("wayonagio_email_agent.kb.retrieve.retrieve", return_value=hits) as mock_retrieve:
            result = runner.invoke(cli, ["kb-search", "machu picchu", "--top-k", "3"])

        assert result.exit_code == 0, result.output
        mock_retrieve.assert_called_once_with("machu picchu", top_k=3)
        assert "0.870" in result.output
        assert "root / a.md" in result.output

    def test_prints_hint_when_no_results(self):
        runner = CliRunner()

        with patch("wayonagio_email_agent.kb.retrieve.retrieve", return_value=[]):
            result = runner.invoke(cli, ["kb-search", "nothing", "--top-k", "3"])

        assert result.exit_code == 0, result.output
        assert "no matches" in result.output.lower()

    def test_kb_unavailable_surfaces_clean_error(self):
        """A missing artifact must produce a one-line error and a non-zero
        exit code, NOT a Python traceback."""
        runner = CliRunner()

        from wayonagio_email_agent.kb.retrieve import KBUnavailableError

        with patch(
            "wayonagio_email_agent.kb.retrieve.retrieve",
            side_effect=KBUnavailableError(
                "KB index artifact could not be downloaded. Run `kb-ingest`."
            ),
        ):
            result = runner.invoke(cli, ["kb-search", "anything"])

        assert result.exit_code != 0
        # ClickException is the clean exit path; any other exception type is
        # a regression (means we let the underlying error escape unwrapped).
        assert result.exception is None or isinstance(
            result.exception, (SystemExit, click.ClickException)
        ), f"Unexpected exception type: {type(result.exception).__name__}"
        assert "kb-ingest" in result.output
        assert "Traceback" not in result.output

    def test_kb_config_error_surfaces_clean_error(self):
        runner = CliRunner()

        from wayonagio_email_agent.kb.config import KBConfigError

        with patch(
            "wayonagio_email_agent.kb.retrieve.retrieve",
            side_effect=KBConfigError("KB_RAG_FOLDER_IDS is required."),
        ):
            result = runner.invoke(cli, ["kb-search", "anything"])

        assert result.exit_code != 0
        assert "KB_RAG_FOLDER_IDS" in result.output
        assert "Traceback" not in result.output


class TestExemplarListCommand:
    def test_prints_each_exemplar_with_title_and_preview(self):
        from wayonagio_email_agent.exemplars.source import Exemplar

        runner = CliRunner()
        sample = [
            Exemplar(title="Refund policy", text="Hello, thank you for...", source_id="d1"),
            Exemplar(title="Altitude tips", text="Many of our clients arrive...", source_id="d2"),
        ]

        with patch(
            "wayonagio_email_agent.exemplars.loader.get_all_exemplars",
            return_value=sample,
        ):
            result = runner.invoke(cli, ["exemplar-list"])

        assert result.exit_code == 0, result.output
        assert "Refund policy" in result.output
        assert "Altitude tips" in result.output
        assert "d1" in result.output
        assert "d2" in result.output
        assert "Hello, thank you for..." in result.output

    def test_disabled_or_empty_prints_actionable_hint(self):
        runner = CliRunner()
        with patch(
            "wayonagio_email_agent.exemplars.loader.get_all_exemplars",
            return_value=[],
        ):
            result = runner.invoke(cli, ["exemplar-list"])

        assert result.exit_code == 0, result.output
        assert "KB_EXEMPLAR_FOLDER_IDS" in result.output

    def test_truncates_long_preview(self):
        from wayonagio_email_agent.exemplars.source import Exemplar

        runner = CliRunner()
        long_body = "X" * 1000
        with patch(
            "wayonagio_email_agent.exemplars.loader.get_all_exemplars",
            return_value=[Exemplar(title="Big", text=long_body, source_id="d")],
        ):
            result = runner.invoke(cli, ["exemplar-list", "--preview-chars", "50"])

        assert result.exit_code == 0
        assert "..." in result.output
        # The full body must NOT appear at the configured cap.
        assert "X" * 1000 not in result.output

    def test_unexpected_loader_failure_surfaces_clean_error(self):
        """Defensive: the loader is contracted not to raise, but if a
        future bug or refactor lets one through, the operator must see a
        clean one-line error rather than a Python traceback."""
        runner = CliRunner()
        with patch(
            "wayonagio_email_agent.exemplars.loader.get_all_exemplars",
            side_effect=RuntimeError("loader misbehaved"),
        ):
            result = runner.invoke(cli, ["exemplar-list"])

        assert result.exit_code != 0
        assert "Could not load exemplars" in result.output
        assert "Traceback" not in result.output
