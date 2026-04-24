"""Drive-folder source for exemplar Docs.

Walks every folder listed in :class:`ExemplarConfig.folder_ids`, fetches each
discovered Doc in parallel, extracts its text, sanitizes it, and returns a
list of :class:`Exemplar` objects ready for prompt injection.

Per-Doc failures are intentionally non-fatal: a single broken Doc must not
disable the whole exemplar pool. Failures are logged at WARNING and the
remaining Docs continue. Empty (post-strip) Docs are skipped with a WARNING
so the curator gets a hint to add content or remove the placeholder.

Performance contract — see ``exemplars/__init__.py`` and
``exemplars/loader.py`` for the cold-start / warm-up story:

* Folder *listing* is sequential. There are typically 1–3 configured folders
  and ``files().list`` is a single round-trip per page; parallelizing
  listing rarely buys anything and complicates pagination.
* Per-Doc *reads* are parallelized through a
  ``ThreadPoolExecutor(max_workers=8)``. Each worker creates its own Drive
  client instead of sharing one client across threads — this is a little more
  auth overhead, but avoids thread-safety issues seen in some environments and
  keeps startup stable. The pool size is still small enough to stay under
  Drive's per-user quota even when warm-up coincides with the first user
  request.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from wayonagio_email_agent.exemplars.config import ExemplarConfig
from wayonagio_email_agent.exemplars.sanitize import sanitize
from wayonagio_email_agent.kb import drive as kb_drive
from wayonagio_email_agent.kb import extract as kb_extract

logger = logging.getLogger(__name__)

_MAX_WORKERS = 8


@dataclass(frozen=True)
class Exemplar:
    """A single curator-written exemplar Doc, ready for prompt injection.

    ``title`` is the Drive Doc name (used as the example heading in the
    prompt and printed by ``cli exemplar-list``). ``text`` is the
    *post-sanitize* body — callers must not have to remember to sanitize
    again. ``source_id`` is the Drive file ID, kept for log correlation
    when an operator asks "which Doc produced this output?".
    """

    title: str
    text: str
    source_id: str


def collect(
    cfg: ExemplarConfig,
    *,
    service: Any | None = None,
    max_workers: int = _MAX_WORKERS,
) -> list[Exemplar]:
    """Collect every exemplar Doc under the configured Drive folders.

    Returns an empty list when *cfg* is disabled — this keeps the loader's
    "any failure → cache []" behavior simple, since "disabled" and "Drive
    returned nothing" are indistinguishable to the caller and should be.

    The *service* and *max_workers* parameters exist for tests. Production
    callers should let both default.
    """
    if not cfg.enabled:
        return []

    svc = service if service is not None else kb_drive.build_drive_service()

    drive_files: list[kb_drive.DriveFile] = []
    for folder_id in cfg.folder_ids:
        drive_files.extend(
            kb_drive.list_folder(
                folder_id,
                recursive=True,
                include_mime_types=cfg.include_mime_types,
                service=svc,
            )
        )

    if not drive_files:
        logger.warning(
            "No exemplar Docs found under KB_EXEMPLAR_FOLDER_IDS — "
            "the curator may have moved or emptied the folders."
        )
        return []

    results: list[Exemplar] = []
    # ``max_workers`` is at least 1 so a worst-case mocked test with a single
    # Doc still uses the threadpool path (and we exercise the same code in
    # tests as in production).
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        per_doc_service = service if service is not None else None
        futures = {
            pool.submit(_load_one, df, per_doc_service): df for df in drive_files
        }
        for future in as_completed(futures):
            df = futures[future]
            try:
                exemplar = future.result()
            except Exception as exc:  # noqa: BLE001 — Drive/extract errors vary widely
                logger.warning(
                    "Failed to load exemplar %s (%s): %s",
                    df.path,
                    df.id,
                    exc,
                )
                continue
            if exemplar is not None:
                results.append(exemplar)

    # Stable, human-readable ordering (by title) so prompt construction is
    # deterministic across runs even though ``as_completed`` returns in
    # whatever order Drive happened to respond.
    results.sort(key=lambda ex: ex.title.lower())
    return results


def _load_one(
    drive_file: kb_drive.DriveFile,
    service: Any | None = None,
) -> Exemplar | None:
    """Fetch, extract, and sanitize one Drive file. Returns ``None`` for
    files that are empty after extraction so the caller can drop them."""
    payload = kb_drive.read_file(drive_file, service=service)
    raw = kb_extract.extract_text(drive_file, payload).strip()
    if not raw:
        logger.warning(
            "Skipping empty exemplar Doc: %s (%s).",
            drive_file.path,
            drive_file.id,
        )
        return None
    return Exemplar(
        title=drive_file.name,
        text=sanitize(raw),
        source_id=drive_file.id,
    )


__all__ = ["Exemplar", "collect"]
