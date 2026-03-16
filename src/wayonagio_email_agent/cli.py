"""Admin CLI.

Usage:
  uv run python -m wayonagio_email_agent.cli auth
  uv run python -m wayonagio_email_agent.cli list [--max N]
  uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
  uv run python -m wayonagio_email_agent.cli scan [--interval N] [--dry-run]
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
    """List recent emails matching a Gmail query."""
    from wayonagio_email_agent import gmail_client

    messages = gmail_client.list_messages(q=query, max_results=max_results)
    if not messages:
        click.echo("No messages found.")
        return

    for msg in messages:
        message_id = msg["id"]
        try:
            full = gmail_client.get_message(message_id)
            parts = gmail_client.extract_message_parts(full)
            click.echo(
                f"[{message_id}] From: {parts['from_']!r}  Subject: {parts['subject']!r}"
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(f"[{message_id}] Error fetching details: {exc}")


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

    agent.scan_loop(interval=interval, dry_run=dry_run)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
