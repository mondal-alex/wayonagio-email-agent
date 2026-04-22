#!/usr/bin/env python3
"""Redact PII in exemplar email text using the same rules as the running agent.

After redaction, :func:`tidy_exemplar_export` (unless ``--raw``) cleans
newlines/NBSPs, collapses repeated Gmail *print* *Correo de…* headers, and
inserts **Message 2, 3, …** dividers before lines that look like a new
*From* bar (Spanish *… de 2026 a las…*, English *On … wrote:*, etc.).

Uses :func:`wayonagio_email_agent.exemplars.sanitize.sanitize` (BOOKING_URL,
EMAIL, IBAN, CARD, PHONE). Person names are **not** auto-detected (that would
strip place names and tour titles); add ``--name`` or ``--names-file``, then
each listed phrase is replaced by a **dummy** (rotating synthetic names by
default, or ``Cliente 1…``, or ``<NAME>`` — see ``--dummies``).

Typical use:
  # Paste thread into a .txt, then:
  uv run python scripts/redact_exemplar_pii.py my_thread.txt -o my_thread_redacted.txt

  # Pipe:
  pbpaste | uv run python scripts/redact_exemplar_pii.py - > out.txt

  # Read directly from a Google Doc (Drive OAuth, same as kb-ingest):
  uv run python scripts/redact_exemplar_pii.py --gdoc 'https://docs.google.com/document/d/FILE_ID/edit' -o redacted.txt

  # With people’s names → dummy stand-ins (default: pool of fictive names):
  uv run python scripts/redact_exemplar_pii.py thread.txt -o out.txt --name "Marco Bianchi" --name "Luigi Verdi"

  # Single placeholder for every name:
  uv run python scripts/redact_exemplar_pii.py thread.txt -o out.txt --name "A B" --dummies=marker
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

# Allow `uv run python scripts/...` without setting PYTHONPATH.
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

# Fictive stand-ins (IT/ES/EN-flavour) — clearly not real customers; cycled
# when more names are listed than the pool size.
_DUMMY_PERSONA_POOL: tuple[str, ...] = (
    "Elena Fabbri",
    "Mauro Neri",
    "Carmen Delgado",
    "Luis Fuentes",
    "Giulia Pellegrini",
    "Tommaso Serra",
    "Ana Ortega",
    "Diego Cuesta",
    "Francesca Motta",
    "Andrea Moretti",
    "Sofía Renedo",
    "Javier Mira",
    "Lucia Brambilla",
    "Pietro Cattaneo",
    "Marta Sánchez",
    "Hugo Paredes",
)


def _dedupe_preserve_phrases(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        s = (raw or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _name_to_dummy_pairs(
    names: list[str], *, dummies: str
) -> list[tuple[str, str]]:
    unique = _dedupe_preserve_phrases(names)
    if not unique:
        return []
    if dummies == "marker":
        return [(p, "<NAME>") for p in unique]
    if dummies == "numbered":
        return [(p, f"Cliente {i + 1}") for i, p in enumerate(unique)]
    n_pool = len(_DUMMY_PERSONA_POOL)
    return [
        (p, _DUMMY_PERSONA_POOL[i % n_pool]) for i, p in enumerate(unique)
    ]


def _parse_gdoc_id(value: str) -> str:
    """Accept a raw Drive file ID or a docs.google.com share/edit URL."""
    s = value.strip()
    if not s:
        return ""
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s


def _redact_gdoc_file_id(gdoc: str) -> str:
    from wayonagio_email_agent.kb.drive import export_doc_as_text

    return export_doc_as_text(_parse_gdoc_id(gdoc))


def _load_names_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _redact_text(
    text: str, names: list[str], *, dummies: str, tidy: bool
) -> str:
    from wayonagio_email_agent.exemplars.sanitize import (
        redact_phrase_map,
        sanitize,
        tidy_exemplar_export,
    )

    cleaned = sanitize(text)
    pairs = _name_to_dummy_pairs(names, dummies=dummies)
    if pairs:
        cleaned = redact_phrase_map(cleaned, pairs)
    if tidy:
        cleaned = tidy_exemplar_export(cleaned)
    return cleaned


def _write_out(data: str, out: Path | None) -> None:
    if out is None:
        sys.stdout.write(data)
        if not data.endswith("\n"):
            sys.stdout.write("\n")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(data, encoding="utf-8")
    click.echo(f"Wrote {out}", err=True)


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    epilog=(
        "Each --name must include a value, e.g. --name 'Giomara Quispe'. "
        "A bare --name at the end of the line is invalid and is ignored by the shell."
    ),
)
@click.argument(
    "inputs",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    metavar="[FILE]...",
)
@click.option(
    "-o",
    "--output",
    "output_file",
    type=click.Path(path_type=Path),
    help="Output path (single file or stdin). Default: print to stdout.",
)
@click.option(
    "-O",
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="For multiple FILE arguments: write <stem>.redacted.txt here.",
)
@click.option(
    "--gdoc",
    "gdoc",
    metavar="ID_OR_URL",
    help="Export this Google Doc as plain text, redact, then write or print.",
)
@click.option(
    "--name",
    "name_cli",
    multiple=True,
    help="Person or phrase to replace with a dummy (see --dummies) (repeatable).",
)
@click.option(
    "--names-file",
    "names_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="One name/phrase per line; # starts a comment. Merged with --name.",
)
@click.option(
    "--dummies",
    "dummies",
    type=click.Choice(["pool", "numbered", "marker"], case_sensitive=False),
    default="pool",
    show_default=True,
    help="pool=rotating fictive full names; numbered=Cliente 1,2,…; marker=<NAME> for all.",
)
@click.option(
    "--raw",
    is_flag=True,
    help="No tidy: skip NBSP/blank rules, *Correo* elision, and *Message* banners.",
)
def main(
    inputs: tuple[Path, ...],
    output_file: Path | None,
    output_dir: Path | None,
    gdoc: str | None,
    name_cli: tuple[str, ...],
    names_file: Path | None,
    dummies: str,
    raw: bool,
) -> None:
    load_dotenv()
    name_list: list[str] = list(name_cli)
    if names_file is not None:
        name_list.extend(_load_names_file(names_file))

    if gdoc is not None and inputs:
        raise click.BadParameter("Use either --gdoc or input FILE(s), not both.")

    if gdoc is not None:
        if output_dir is not None:
            raise click.BadParameter("--output-dir is not used with --gdoc.")
        try:
            doc_text = _redact_gdoc_file_id(gdoc)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(f"Failed to read Google Doc: {exc}") from exc
        if not name_list:
            click.echo(
                "Note: no --name or --names-file — only PII tokens are substituted; "
                "add e.g. --name \"Giomara Quispe\" (repeat per person) for dummies.",
                err=True,
            )
        _write_out(
            _redact_text(
                doc_text, name_list, dummies=dummies, tidy=not raw
            ),
            output_file,
        )
        return

    if not inputs:
        if output_dir is not None:
            raise click.BadParameter("--output-dir requires at least one FILE.")
        text = sys.stdin.read()
        _write_out(
            _redact_text(text, name_list, dummies=dummies, tidy=not raw),
            output_file,
        )
        return

    if len(inputs) == 1:
        if output_dir is not None:
            raise click.BadParameter("With one FILE, use -o/--output, not -O/--output-dir.")
        path = inputs[0]
        text = path.read_text(encoding="utf-8")
        _write_out(
            _redact_text(text, name_list, dummies=dummies, tidy=not raw),
            output_file,
        )
        return

    if output_dir is None:
        raise click.BadParameter("Multiple files require -O/--output-dir.")
    if output_file is not None:
        raise click.BadParameter("Use -O/--output-dir with multiple files, not -o.")
    out_dir = output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in inputs:
        name = f"{path.stem}.redacted{path.suffix}"
        dest = out_dir / name
        text = path.read_text(encoding="utf-8")
        dest.write_text(
            _redact_text(
                text, name_list, dummies=dummies, tidy=not raw
            ),
            encoding="utf-8",
        )
        click.echo(f"Wrote {dest}", err=True)


if __name__ == "__main__":
    main()
