"""Unit tests for cli.py."""

from __future__ import annotations

from unittest.mock import patch

import click
from click.testing import CliRunner

from wayonagio_email_agent.cli import cli


class TestAuthCommand:
    """`cli auth` runs the interactive Gmail OAuth flow. The flow itself
    needs a real browser, so in tests we just assert the CLI wires through
    to ``gmail_client.run_auth_flow`` and reports success cleanly.
    """

    def test_runs_auth_flow_and_echoes_success(self):
        runner = CliRunner()

        with patch(
            "wayonagio_email_agent.gmail_client.run_auth_flow"
        ) as mock_flow:
            result = runner.invoke(cli, ["auth"])

        assert result.exit_code == 0, result.output
        mock_flow.assert_called_once_with()
        assert "Authentication complete" in result.output


class TestListCommand:
    """`cli list` is the operator's smoke-test for Gmail credentials: it
    lists the N most recent (or queried) messages with their From/Subject
    headers. The CLI wires through to the batched Gmail metadata call so
    this is a single round-trip regardless of N.
    """

    def test_prints_message_rows(self):
        runner = CliRunner()
        messages = [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}]
        metadata = [
            {"id": "m1", "from_": "alice@example.com", "subject": "Tour inquiry"},
            {"id": "m2", "from_": "bob@example.com", "subject": "Refund question"},
        ]
        with (
            patch(
                "wayonagio_email_agent.gmail_client.list_messages",
                return_value=messages,
            ),
            patch(
                "wayonagio_email_agent.gmail_client.get_messages_metadata",
                return_value=metadata,
            ) as mock_meta,
        ):
            result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0, result.output
        # The batched metadata call must receive the list of IDs — that's
        # the N+1 avoidance we care about.
        mock_meta.assert_called_once_with(["m1", "m2"])
        assert "alice@example.com" in result.output
        assert "bob@example.com" in result.output
        assert "Tour inquiry" in result.output
        assert "Refund question" in result.output

    def test_empty_result_prints_friendly_message(self):
        runner = CliRunner()
        with patch(
            "wayonagio_email_agent.gmail_client.list_messages", return_value=[]
        ):
            result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "No messages found" in result.output

    def test_per_row_error_is_surfaced_without_aborting(self):
        """One bad metadata fetch must not hide the rest of the list.
        The CLI renders a row-level error marker and continues with the
        successful rows — the operator can still see what's in the inbox.
        """
        runner = CliRunner()
        messages = [{"id": "ok", "threadId": "t"}, {"id": "bad", "threadId": "t2"}]
        metadata = [
            {"id": "ok", "from_": "alice@example.com", "subject": "Hi"},
            {"id": "bad", "error": "notFound"},
        ]
        with (
            patch(
                "wayonagio_email_agent.gmail_client.list_messages",
                return_value=messages,
            ),
            patch(
                "wayonagio_email_agent.gmail_client.get_messages_metadata",
                return_value=metadata,
            ),
        ):
            result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "Hi" in result.output
        assert "Error fetching details" in result.output
        assert "notFound" in result.output

    def test_forwards_custom_query_and_max(self):
        runner = CliRunner()
        with (
            patch(
                "wayonagio_email_agent.gmail_client.list_messages",
                return_value=[],
            ) as mock_list,
            patch(
                "wayonagio_email_agent.gmail_client.get_messages_metadata",
                return_value=[],
            ),
        ):
            result = runner.invoke(
                cli, ["list", "--query", "label:travel", "--max", "5"]
            )

        assert result.exit_code == 0
        mock_list.assert_called_once_with(q="label:travel", max_results=5)


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

        # Use a realistic message: real KBUnavailableError instances carry
        # their own actionable hint (see ``kb/retrieve._load_state``), so the
        # CLI just passes the exception text through verbatim.
        with patch(
            "wayonagio_email_agent.agent.manual_draft_flow",
            side_effect=KBUnavailableError(
                "KB index artifact could not be downloaded. Run `kb-ingest` "
                "to publish kb_index.sqlite."
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


class TestKBDoctorCommand:
    """``kb-doctor`` is the one-shot health check. These tests anchor:

    * healthy report → exit 0, expected sections printed,
    * unhealthy report → non-zero exit (safe to wire into deploy smoke
      tests / readiness probes) + actionable issues visible,
    * KBConfigError (missing KB_RAG_FOLDER_IDS) → clean one-line error,
      not a Python traceback,
    * --max-sources is forwarded to the formatter.
    """

    def _fake_report(self, **overrides):
        from wayonagio_email_agent.kb.doctor import DoctorReport

        defaults = dict(
            rag_folder_count=2,
            embedding_model="gemini/gemini-embedding-001",
            top_k=4,
            artifact_destination="gs://bucket/kb_index.sqlite",
            index_filename="kb_index.sqlite",
            artifact_available=True,
            index_loaded=True,
            index_meta=None,
            chunk_count=10,
            sources=[],
            embedding_model_matches=True,
            exemplar_count=0,
            exemplar_titles=[],
            issues=[],
        )
        defaults.update(overrides)
        return DoctorReport(**defaults)

    def test_healthy_report_exits_zero_and_prints_sections(self):
        runner = CliRunner()
        report = self._fake_report()

        with patch(
            "wayonagio_email_agent.kb.doctor.build_report", return_value=report
        ):
            result = runner.invoke(cli, ["kb-doctor"])

        assert result.exit_code == 0, result.output
        assert "KB status: HEALTHY" in result.output
        assert "Config:" in result.output
        assert "Index:" in result.output
        assert "Exemplars:" in result.output

    def test_unhealthy_report_exits_non_zero_with_issues(self):
        """Unhealthy => non-zero exit. This is the contract that makes
        ``kb-doctor`` safe to use as a deploy smoke-test or readiness
        probe: ``cli kb-doctor || exit 1`` will correctly trip CI when
        the KB is broken."""
        runner = CliRunner()
        report = self._fake_report(
            artifact_available=False,
            index_loaded=False,
            chunk_count=0,
            embedding_model_matches=False,
            issues=["KB artifact not found. Run `kb-ingest` to publish it."],
        )

        with patch(
            "wayonagio_email_agent.kb.doctor.build_report", return_value=report
        ):
            result = runner.invoke(cli, ["kb-doctor"])

        assert result.exit_code != 0
        assert "KB status: UNHEALTHY" in result.output
        assert "kb-ingest" in result.output
        assert "Traceback" not in result.output

    def test_config_error_surfaces_clean_error(self):
        runner = CliRunner()

        from wayonagio_email_agent.kb.config import KBConfigError

        with patch(
            "wayonagio_email_agent.kb.doctor.build_report",
            side_effect=KBConfigError("KB_RAG_FOLDER_IDS is required."),
        ):
            result = runner.invoke(cli, ["kb-doctor"])

        assert result.exit_code != 0
        assert "KB_RAG_FOLDER_IDS" in result.output
        assert "Traceback" not in result.output

    def test_unexpected_error_is_wrapped_as_clickexception(self):
        runner = CliRunner()

        with patch(
            "wayonagio_email_agent.kb.doctor.build_report",
            side_effect=RuntimeError("unexpected boom"),
        ):
            result = runner.invoke(cli, ["kb-doctor"])

        assert result.exit_code != 0
        assert "unexpected boom" in result.output
        assert "Traceback" not in result.output

    def test_max_sources_flag_is_forwarded_to_formatter(self):
        runner = CliRunner()
        report = self._fake_report()

        with (
            patch(
                "wayonagio_email_agent.kb.doctor.build_report",
                return_value=report,
            ),
            patch(
                "wayonagio_email_agent.kb.doctor.format_report",
                return_value="stub\n",
            ) as mock_format,
        ):
            result = runner.invoke(cli, ["kb-doctor", "--max-sources", "5"])

        assert result.exit_code == 0
        mock_format.assert_called_once_with(report, max_sources=5)


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
