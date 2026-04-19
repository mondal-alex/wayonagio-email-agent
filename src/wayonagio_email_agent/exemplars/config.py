"""Exemplar configuration.

All exemplar tunables come from environment variables, mirroring ``kb/config``.
Unlike the KB, exemplars are **optional**: when ``KB_EXEMPLAR_FOLDER_IDS`` is
unset or empty, ``load()`` returns a config with ``enabled=False`` rather than
raising. The runtime treats "disabled" as the steady, no-op state — the prompt
is built without an ``EXAMPLE RESPONSES`` block and no Drive call is ever made.

Drive folder IDs are normalized through ``kb.config.parse_folder_id`` so
operators can paste either raw IDs or share URLs, identical to the KB UX.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from wayonagio_email_agent.kb.config import parse_folder_id

DEFAULT_INCLUDE_MIME_TYPES = (
    "application/pdf",
    "application/vnd.google-apps.document",
    "text/plain",
    "text/markdown",
)


def _parse_csv_folder_ids(raw: str) -> list[str]:
    return [parse_folder_id(part) for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class ExemplarConfig:
    """Snapshot of exemplar configuration resolved from the environment."""

    folder_ids: tuple[str, ...]
    include_mime_types: tuple[str, ...]

    @property
    def enabled(self) -> bool:
        """``True`` iff at least one Drive folder ID is configured.

        Derived rather than stored so the answer is always consistent with
        ``folder_ids`` — there is no path where the two can disagree.
        """
        return bool(self.folder_ids)


def load() -> ExemplarConfig:
    """Resolve an :class:`ExemplarConfig` snapshot from the current environment.

    Re-reads ``os.environ`` on every call so tests (and live config changes
    on long-lived processes) are reflected without import-time caching
    surprises.

    Does **not** raise when ``KB_EXEMPLAR_FOLDER_IDS`` is missing — exemplars
    are an opt-in feature. The caller (typically ``loader.get_all_exemplars``)
    short-circuits cleanly on ``cfg.enabled is False``.
    """
    folder_ids = tuple(
        _parse_csv_folder_ids(os.environ.get("KB_EXEMPLAR_FOLDER_IDS", ""))
    )

    mime_raw = os.environ.get("KB_EXEMPLAR_INCLUDE_MIME_TYPES", "")
    if mime_raw.strip():
        include_mime = tuple(m.strip() for m in mime_raw.split(",") if m.strip())
    else:
        include_mime = DEFAULT_INCLUDE_MIME_TYPES

    return ExemplarConfig(
        folder_ids=folder_ids,
        include_mime_types=include_mime,
    )
