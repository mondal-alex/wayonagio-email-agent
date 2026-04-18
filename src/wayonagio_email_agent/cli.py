"""Admin CLI.

Usage:
  uv run python -m wayonagio_email_agent.cli auth
  uv run python -m wayonagio_email_agent.cli list [--max N]
  uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
  uv run python -m wayonagio_email_agent.cli scan [--interval N] [--dry-run]
  uv run python -m wayonagio_email_agent.cli scan-once [--dry-run]
  uv run python -m wayonagio_email_agent.cli kb-ingest
  uv run python -m wayonagio_email_agent.cli kb-search <query> [--top-k N]
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

    draft = agent.manual_draft_flow(message_id)
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

    hits = kb_retrieve.retrieve(query, top_k=top_k)
    if not hits:
        click.echo("No results. Is KB_ENABLED=true and has kb-ingest been run?")
        return

    for i, hit in enumerate(hits, 1):
        click.echo(f"[{i}] score={hit.score:.3f}  source={hit.source_path}")
        preview = hit.text.replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:237] + "..."
        click.echo(f"    {preview}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
