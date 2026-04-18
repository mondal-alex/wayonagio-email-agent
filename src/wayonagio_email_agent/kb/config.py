"""KB configuration.

All KB tunables come from environment variables so operators can change
behavior without rebuilding or redeploying the container.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Default MIME types we know how to extract text from. Anything else in the
# configured Drive folders is silently skipped, so mixed-content folders are
# fine to point at. Extend via KB_INCLUDE_MIME_TYPES when needed.
DEFAULT_INCLUDE_MIME_TYPES = (
    "application/pdf",
    "application/vnd.google-apps.document",
    "text/plain",
    "text/markdown",
)

_DEFAULT_EMBEDDING_MODEL = "gemini/text-embedding-004"
_DEFAULT_ARTIFACT_DIR = "./kb_artifacts"
_INDEX_FILENAME = "kb_index.sqlite"


# ---------------------------------------------------------------------------
# Primitive parsers
# ---------------------------------------------------------------------------

_DRIVE_URL_PATTERNS = (
    re.compile(r"/folders/([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
)


def parse_folder_id(value: str) -> str:
    """Accept a raw Drive folder ID *or* a share URL and return the ID.

    Operators naturally paste share links out of the Drive UI. Handling both
    shapes means nobody has to learn how to extract the ID manually.
    """
    value = value.strip()
    if not value:
        return ""
    for pattern in _DRIVE_URL_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value


def _parse_csv_folder_ids(raw: str) -> list[str]:
    return [parse_folder_id(part) for part in raw.split(",") if part.strip()]


def _parse_bool(raw: str, *, default: bool) -> bool:
    if raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Public config object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KBConfig:
    """Snapshot of KB configuration resolved from the environment."""

    enabled: bool
    rag_folder_ids: tuple[str, ...]
    rag_recursive: bool
    include_mime_types: tuple[str, ...]
    embedding_model: str
    gcs_uri: str
    local_dir: str
    top_k: int

    @property
    def index_filename(self) -> str:
        return _INDEX_FILENAME


def load() -> KBConfig:
    """Resolve a :class:`KBConfig` snapshot from the current environment.

    Re-reads ``os.environ`` on every call so tests (and live config changes on
    long-lived processes) are reflected without import-time caching surprises.
    """
    enabled = _parse_bool(os.environ.get("KB_ENABLED", ""), default=False)

    rag_ids = tuple(_parse_csv_folder_ids(os.environ.get("KB_RAG_FOLDER_IDS", "")))

    mime_raw = os.environ.get("KB_INCLUDE_MIME_TYPES", "")
    if mime_raw.strip():
        include_mime = tuple(m.strip() for m in mime_raw.split(",") if m.strip())
    else:
        include_mime = DEFAULT_INCLUDE_MIME_TYPES

    top_k_raw = os.environ.get("KB_TOP_K", "").strip()
    try:
        top_k = max(1, int(top_k_raw)) if top_k_raw else 4
    except ValueError:
        top_k = 4

    return KBConfig(
        enabled=enabled,
        rag_folder_ids=rag_ids,
        rag_recursive=_parse_bool(
            os.environ.get("KB_RAG_RECURSIVE", ""), default=True
        ),
        include_mime_types=include_mime,
        embedding_model=os.environ.get("KB_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL),
        gcs_uri=os.environ.get("KB_GCS_URI", "").strip(),
        local_dir=os.environ.get("KB_LOCAL_DIR", _DEFAULT_ARTIFACT_DIR).strip()
        or _DEFAULT_ARTIFACT_DIR,
        top_k=top_k,
    )
