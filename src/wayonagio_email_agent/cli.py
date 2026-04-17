"""Admin CLI.

Usage:
  uv run python -m wayonagio_email_agent.cli auth
  uv run python -m wayonagio_email_agent.cli list [--max N]
  uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
  uv run python -m wayonagio_email_agent.cli scan [--interval N] [--dry-run]
  uv run python -m wayonagio_email_agent.cli scan-once [--dry-run]
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
