"""Artifact I/O for the KB.

At runtime the API process needs the ``kb_index.sqlite`` vector store. It
lives either in a **GCS bucket** (production on Cloud Run — set via
``KB_GCS_URI``) or on the **local filesystem** (dev / CI — set via
``KB_LOCAL_DIR``, default ``./kb_artifacts/``). The ingest Job writes it,
the API process reads it.

Keeping the read and write sides in one module means there is exactly one
place in the codebase that knows how to locate an artifact.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from wayonagio_email_agent.kb.config import KBConfig

logger = logging.getLogger(__name__)


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Return (bucket, prefix) for a ``gs://bucket/prefix`` URI.

    A trailing ``/`` on *prefix* is ignored. Prefix may be empty (objects land
    at the bucket root).
    """
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"KB_GCS_URI must look like gs://bucket[/prefix]; got {uri!r}")
    prefix = parsed.path.lstrip("/").rstrip("/")
    return parsed.netloc, prefix


def _gcs_object_name(prefix: str, filename: str) -> str:
    if not prefix:
        return filename
    return f"{prefix}/{filename}"


def _local_path(config: KBConfig, filename: str) -> Path:
    return Path(config.local_dir) / filename


# ---------------------------------------------------------------------------
# Write (ingest)
# ---------------------------------------------------------------------------

def upload_artifact(config: KBConfig, local_path: Path, filename: str) -> str:
    """Publish *local_path* as *filename* to the configured destination.

    If ``KB_GCS_URI`` is set we upload to GCS; otherwise we copy into
    ``KB_LOCAL_DIR`` (which is useful for integration tests and single-host
    deployments). Returns a human-readable description of where the file
    landed, for logging.
    """
    if config.gcs_uri:
        bucket_name, prefix = _parse_gcs_uri(config.gcs_uri)
        # Imported locally so the runtime path (which only reads) doesn't have
        # to import the storage client just to load the module.
        from google.cloud import storage  # type: ignore[import-not-found]

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(_gcs_object_name(prefix, filename))
        blob.upload_from_filename(str(local_path))
        destination = f"gs://{bucket_name}/{_gcs_object_name(prefix, filename)}"
        logger.info("Uploaded %s -> %s (%d bytes).", filename, destination, local_path.stat().st_size)
        return destination

    destination = _local_path(config, filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(local_path.read_bytes())
    logger.info("Wrote %s -> %s.", filename, destination)
    return str(destination)


# ---------------------------------------------------------------------------
# Read (runtime)
# ---------------------------------------------------------------------------

def download_artifact(config: KBConfig, filename: str, dest_dir: Path) -> Path | None:
    """Fetch *filename* into *dest_dir* and return the local path.

    Returns ``None`` when the artifact is not available (no object in GCS,
    or no file in the local artifact dir). The KB is a hard dependency of
    every draft, so callers (``kb.retrieve._load_state``, ``kb.doctor``)
    treat ``None`` as a failure and raise ``KBUnavailableError`` / surface
    an issue in the health report. We still return ``None`` rather than
    raising so the caller controls the error message (it has more context
    about whether retry, reingest, or configuration is the remedy).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    local_target = dest_dir / filename

    if config.gcs_uri:
        try:
            from google.cloud import storage  # type: ignore[import-not-found]

            bucket_name, prefix = _parse_gcs_uri(config.gcs_uri)
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(_gcs_object_name(prefix, filename))
            if not blob.exists():
                logger.warning(
                    "KB artifact %s not found at gs://%s/%s.",
                    filename,
                    bucket_name,
                    _gcs_object_name(prefix, filename),
                )
                return None
            blob.download_to_filename(str(local_target))
            logger.info(
                "Downloaded %s from gs://%s/%s.",
                filename,
                bucket_name,
                _gcs_object_name(prefix, filename),
            )
            return local_target
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to download %s from GCS: %s", filename, exc)
            return None

    source = _local_path(config, filename)
    if not source.exists():
        logger.warning("KB artifact %s not found at %s.", filename, source)
        return None
    local_target.write_bytes(source.read_bytes())
    return local_target
