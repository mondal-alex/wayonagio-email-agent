"""Unit tests for cli.py."""

from __future__ import annotations

from unittest.mock import patch

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
        assert "KB_ENABLED" in result.output
