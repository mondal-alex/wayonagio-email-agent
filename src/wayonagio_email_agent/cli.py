"""Admin CLI.

Usage:
  uv run python -m wayonagio_email_agent.cli auth
  uv run python -m wayonagio_email_agent.cli list [--max N]
  uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
  uv run python -m wayonagio_email_agent.cli scan [--interval N] [--dry-run]
  uv run python -m wayonagio_email_agent.cli scan-once [--dry-run]
  uv run python -m wayonagio_email_agent.cli kb-ingest
  uv run python -m wayonagio_email_agent.cli kb-search <query> [--top-k N]
  uv run python -m wayonagio_email_agent.cli kb-doctor
  uv run python -m wayonagio_email_agent.cli exemplar-list
"""

from __future__ import annotations

import logging
import os

import click
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@click.group()
def cli() -> None:
    """Wayonagio email agent — admin CLI."""


@cli.command()
def auth() -> None:
    """Run interactive OAuth2 flow and save token.json.

    Must be run once on the server before any other commands will work.
    Re-run if the token becomes invalid (e.g. after revoking access).
    """
    from wayonagio_email_agent.gmail_client import run_auth_flow

    run_auth_flow()
    click.echo("Authentication complete.")


@cli.command(name="list")
@click.option("--max", "max_results", default=10, show_default=True, help="Max emails to show.")
@click.option("--query", "-q", default="is:unread", show_default=True, help="Gmail search query.")
def list_emails(max_results: int, query: str) -> None:
    """List recent emails matching a Gmail query.

    Uses a single batched Gmail request to fetch header metadata for all
    matched messages, instead of one ``messages.get`` call per message.
    """
    from wayonagio_email_agent import gmail_client

    messages = gmail_client.list_messages(q=query, max_results=max_results)
    if not messages:
        click.echo("No messages found.")
        return

    ids = [m["id"] for m in messages]
    rows = gmail_client.get_messages_metadata(ids)
    for row in rows:
        if "error" in row:
            click.echo(f"[{row['id']}] Error fetching details: {row['error']}")
            continue
        click.echo(
            f"[{row['id']}] From: {row['from_']!r}  Subject: {row['subject']!r}"
        )


@cli.command(name="draft-reply")
@click.argument("message_id")
def draft_reply(message_id: str) -> None:
    """Create a draft reply for MESSAGE_ID."""
    from wayonagio_email_agent import agent
    from wayonagio_email_agent.kb.config import KBConfigError
    from wayonagio_email_agent.kb.retrieve import KBUnavailableError
    from wayonagio_email_agent.llm.client import EmptyReplyError

    # Translate the same expected runtime errors that the API maps to
    # 503/502 into clean ClickExceptions. Without this, operators running
    # the CLI to debug a Gmail thread see a Python traceback instead of an
    # actionable one-line error pointing at kb-ingest / the LLM provider.
    #
    # The exception messages already contain a remediation hint (e.g. "Run
    # `kb-ingest` to publish kb_index.sqlite ..."), so we pass them through
    # verbatim rather than appending a second, redundant hint.
    try:
        draft = agent.manual_draft_flow(message_id)
    except (KBUnavailableError, KBConfigError) as exc:
        raise click.ClickException(f"Knowledge base unavailable: {exc}")
    except EmptyReplyError as exc:
        raise click.ClickException(f"LLM returned an empty reply: {exc}")
    click.echo(f"Draft created: {draft.get('id')}")


@cli.command()
@click.option(
    "--interval",
    default=1800,
    show_default=True,
    help="Seconds between scans (default: 1800 = 30 min).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Classify and log what would be drafted without creating drafts.",
)
def scan(interval: int, dry_run: bool) -> None:
    """Run the automatic email scanner.

    Continuously polls Gmail for unread travel-related emails and creates
    draft replies. Use --dry-run to test classification without side-effects.
    """
    from wayonagio_email_agent import agent

    if not agent.scanner_enabled():
        raise click.ClickException(
            "Scanner is disabled. Set SCANNER_ENABLED=true to enable automatic scanning."
        )

    agent.scan_loop(interval=interval, dry_run=dry_run)


@cli.command(name="scan-once")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Classify and log what would be drafted without creating drafts.",
)
def scan_once(dry_run: bool) -> None:
    """Run a single scan pass and exit.

    Designed for schedulers that re-invoke the process at a fixed cadence
    (e.g. Google Cloud Scheduler → Cloud Run Jobs, cron, systemd timers).
    Unlike ``scan``, this command does not loop and is safe to run in
    serverless environments that scale to zero between invocations.
    """
    from wayonagio_email_agent import agent

    if not agent.scanner_enabled():
        raise click.ClickException(
            "Scanner is disabled. Set SCANNER_ENABLED=true to enable automatic scanning."
        )

    agent.scan_once(dry_run=dry_run)


@cli.command(name="kb-ingest")
def kb_ingest() -> None:
    """Build and publish the KB vector index.

    Reads every configured RAG Drive folder, extracts text, chunks it, embeds
    the chunks, and writes ``kb_index.sqlite`` to GCS (``KB_GCS_URI``) or the
    local artifact dir (``KB_LOCAL_DIR``). Intended to run as a Cloud Run Job
    triggered by Cloud Scheduler.
    """
    from wayonagio_email_agent.kb import ingest

    result = ingest.run()
    click.echo(
        f"KB ingest complete: rag_sources={result.rag_source_count}, "
        f"chunks={result.rag_chunk_count}, dim={result.embedding_dim}.\n"
        f"  index -> {result.index_destination}"
    )


@cli.command(name="kb-search")
@click.argument("query")
@click.option("--top-k", default=4, show_default=True, help="How many chunks to return.")
def kb_search(query: str, top_k: int) -> None:
    """Debug retrieval: print the top chunks for QUERY.

    Useful for sanity-checking a fresh ingest before a draft goes out.
    """
    from wayonagio_email_agent.kb import retrieve as kb_retrieve
    from wayonagio_email_agent.kb.config import KBConfigError
    from wayonagio_email_agent.kb.retrieve import KBUnavailableError

    # The KB is required: a missing artifact, mismatched embedding model, or
    # unset KB_RAG_FOLDER_IDS raises rather than returning []. Translate to a
    # ClickException so the operator sees a clean one-line error and a
    # non-zero exit code instead of a Python traceback.
    try:
        hits = kb_retrieve.retrieve(query, top_k=top_k)
    except (KBUnavailableError, KBConfigError) as exc:
        raise click.ClickException(str(exc))

    if not hits:
        click.echo("No matches above similarity threshold.")
        return

    for i, hit in enumerate(hits, 1):
        click.echo(f"[{i}] score={hit.score:.3f}  source={hit.source_path}")
        preview = hit.text.replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:237] + "..."
        click.echo(f"    {preview}")


@cli.command(name="kb-doctor")
@click.option(
    "--max-sources",
    default=20,
    show_default=True,
    help="How many of the most-chunked sources to list.",
)
def kb_doctor(max_sources: int) -> None:
    """Print a one-shot health report for the knowledge base.

    Answers the question every on-call operator asks first when drafts
    start 503-ing: "what does the agent actually think is in the KB, and
    when was it last ingested?" Surfaces, in one command:

    * config snapshot (RAG folder count, embedding model, top-K, artifact
      destination),
    * whether the index artifact is present and loadable,
    * ingest timestamp + age, chunk count, per-source chunk breakdown,
    * whether the index's embedding model matches the runtime config,
    * the exemplar pool's size + titles.

    Exits non-zero when the KB is unhealthy (artifact missing, index
    empty, embedding-model mismatch) so this command is safe to wire
    into a readiness probe or a smoke-test step after ``kb-ingest``.
    """
    from wayonagio_email_agent.kb import doctor
    from wayonagio_email_agent.kb.config import KBConfigError

    try:
        report = doctor.build_report()
    except KBConfigError as exc:
        raise click.ClickException(str(exc))
    except Exception as exc:  # noqa: BLE001 — defensive: unknown subsystem failures
        raise click.ClickException(f"kb-doctor failed: {exc}")

    click.echo(doctor.format_report(report, max_sources=max_sources), nl=False)

    if not report.healthy:
        # A failing kb-doctor should break CI / deployment smoke tests,
        # not just print and exit 0. ClickException owns the non-zero
        # exit code and keeps the output we've already printed.
        raise click.ClickException("KB is unhealthy — see report above.")


@cli.command(name="exemplar-list")
@click.option(
    "--preview-chars",
    default=200,
    show_default=True,
    help="How many characters of each exemplar body to print as a preview.",
)
def exemplar_list(preview_chars: int) -> None:
    """List the curator-managed exemplars the agent will inject into prompts.

    A sanity-check command: confirms what exemplar Docs the runtime sees
    after sanitization, in the order they will appear in the EXAMPLE
    RESPONSES block. Useful for verifying that a curator's edit landed and
    that PII redaction is doing its job before drafts go out.

    Mirrors the contract of ``kb-search``: any unexpected exception is
    translated to a clean ``click.ClickException`` so the operator sees a
    one-line error and a non-zero exit code instead of a Python traceback.
    The loader itself never raises, but bypassing it here (we read the
    cache directly via ``get_all_exemplars``) keeps the failure-handling
    surface narrow.
    """
    from wayonagio_email_agent.exemplars import loader as exemplar_loader

    try:
        exemplars = exemplar_loader.get_all_exemplars()
    except Exception as exc:  # noqa: BLE001 — defensive: loader is contracted not to raise
        raise click.ClickException(f"Could not load exemplars: {exc}")

    if not exemplars:
        click.echo(
            "No exemplars loaded. Either KB_EXEMPLAR_FOLDER_IDS is unset, "
            "the configured Drive folder is empty, or the cold-start load "
            "failed (check logs for WARNING)."
        )
        return

    for index, exemplar in enumerate(exemplars, start=1):
        preview = exemplar.text.replace("\n", " ")
        if len(preview) > preview_chars:
            preview = preview[: max(0, preview_chars - 3)] + "..."
        click.echo(f"[{index}] {exemplar.title}  (id={exemplar.source_id})")
        click.echo(f"    {preview}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
