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
