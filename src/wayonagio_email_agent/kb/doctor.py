"""KB health report (``kb-doctor``).

The KB is a hard runtime dependency — if it breaks, every draft breaks
with it — but its failure modes (missing artifact, mismatched embedding
model, empty index, stale ingest, shrunken corpus) are spread across four
subsystems: config, artifact I/O, the on-disk SQLite index, and the
Drive/embeddings pipeline. When an operator pages at 2am because drafts
are 503-ing, they don't want to piece together which one is broken from
``kb-search``'s error message; they want one command that prints every
load-bearing fact at once.

This module builds that report. It is deliberately read-only and
side-effect-light — it does not write to GCS, does not contact Drive, and
does not call the embeddings provider. It only:

* resolves the runtime config,
* tries to download and open the published index artifact, and
* summarizes the contents (meta, chunk count, per-source breakdown)
  alongside the exemplar pool state.

The CLI wrapper in :mod:`cli` formats this report for humans. Other
callers (a future admin HTTP endpoint, a monitoring job) can consume the
dataclass directly.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from wayonagio_email_agent.kb import artifact, config as config_module
from wayonagio_email_agent.kb.store import IndexMeta, load_index

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceStat:
    """One source file as seen by retrieval, with how many chunks it produced."""

    source_path: str
    chunk_count: int


@dataclass
class DoctorReport:
    """Everything an operator needs to judge "is the KB healthy?".

    This is a plain data object — the CLI does all formatting. Extend
    carefully: every field here becomes part of the implicit operator UI,
    and the exemplar section was deliberately kept minimal for the same
    reason ``cli exemplar-list`` is its own command (deep inspection of
    exemplars is not this command's job).
    """

    # Config snapshot — what the runtime thinks it should be looking for.
    rag_folder_count: int
    embedding_model: str
    top_k: int
    artifact_destination: str  # "gs://..." or a filesystem path
    index_filename: str

    # Index artifact + contents.
    artifact_available: bool
    index_loaded: bool
    index_meta: IndexMeta | None
    chunk_count: int
    sources: list[SourceStat]  # sorted by chunk_count desc, then path
    embedding_model_matches: bool

    # Exemplar pool state. Separate pool, separate contract (optional +
    # graceful) — we include it because operators asked for one command
    # that answers "what does the agent see?".
    exemplar_count: int
    exemplar_titles: list[str]

    # Anything that went wrong during report construction that the
    # operator needs to act on. Each string is meant to be a single line
    # printed verbatim, so keep them short and actionable.
    issues: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """The KB is usable by ``llm/client.generate_reply`` right now.

        Exemplars are explicitly excluded from this boolean because
        exemplars are graceful-degradation territory: an empty pool is a
        valid, working state. Only hard KB failures mark the report
        unhealthy.
        """
        return (
            self.artifact_available
            and self.index_loaded
            and self.chunk_count > 0
            and self.embedding_model_matches
        )


def _artifact_destination(cfg: config_module.KBConfig) -> str:
    """Render the publish destination the way the operator configured it.

    We return the GCS URI verbatim when set (so it matches what they see
    in ``.env`` / Secret Manager) and the resolved local path otherwise.
    """
    if cfg.gcs_uri:
        # ``upload_artifact`` appends ``/kb_index.sqlite`` — mirror that
        # so the printed destination is copy-pasteable into ``gsutil``.
        base = cfg.gcs_uri.rstrip("/")
        return f"{base}/{cfg.index_filename}"
    return str(Path(cfg.local_dir) / cfg.index_filename)


def _count_sources(index_path: Path) -> list[SourceStat]:
    """Group the index's chunks by ``source_path`` and return a sorted
    summary.

    We run a direct SQL aggregation instead of reusing :func:`load_index`
    so ``kb-doctor`` stays fast on large indices: summarizing a 10k-chunk
    index should not force us to materialize and L2-normalize a 10k × 768
    float32 matrix in memory.
    """
    with closing(sqlite3.connect(index_path)) as conn:
        rows = conn.execute(
            """
            SELECT source_path, COUNT(*) AS n
            FROM chunks
            GROUP BY source_path
            ORDER BY n DESC, source_path ASC
            """
        ).fetchall()
    return [SourceStat(source_path=r[0], chunk_count=int(r[1])) for r in rows]


def build_report() -> DoctorReport:
    """Produce a :class:`DoctorReport` for the current runtime config.

    This function is deliberately tolerant of partial failures: every
    error that stops us from completing a section is captured in
    ``issues`` and we return a best-effort report rather than raising.
    An operator running ``kb-doctor`` in production wants to see every
    load-bearing fact we could collect, not a traceback from the first
    broken subsystem.

    The one failure we *do* re-raise is :class:`KBConfigError` — a
    missing ``KB_RAG_FOLDER_IDS`` means we can't even ask the question
    ``kb-doctor`` is asking, so the caller gets a clean ``ClickException``
    instead of a half-built report that hides the real problem.
    """
    # ``config_module.load()`` raises KBConfigError when KB_RAG_FOLDER_IDS
    # is unset. We let that propagate — a misconfigured agent should not
    # silently emit a "looks healthy-ish" report.
    cfg = config_module.load()

    report = DoctorReport(
        rag_folder_count=len(cfg.rag_folder_ids),
        embedding_model=cfg.embedding_model,
        top_k=cfg.top_k,
        artifact_destination=_artifact_destination(cfg),
        index_filename=cfg.index_filename,
        artifact_available=False,
        index_loaded=False,
        index_meta=None,
        chunk_count=0,
        sources=[],
        embedding_model_matches=False,
        exemplar_count=0,
        exemplar_titles=[],
    )

    with tempfile.TemporaryDirectory(prefix="kb_doctor_") as tmp:
        cache_dir = Path(tmp)
        try:
            index_path = artifact.download_artifact(cfg, cfg.index_filename, cache_dir)
        except Exception as exc:  # noqa: BLE001 — defensive: GCS client can raise anything
            report.issues.append(
                f"Could not download the KB artifact: {exc}. "
                "Check KB_GCS_URI / credentials, or run `kb-ingest`."
            )
            index_path = None

        if index_path is None:
            if not report.issues:
                report.issues.append(
                    "KB artifact not found at the configured destination. "
                    "Run `kb-ingest` to publish it."
                )
        else:
            report.artifact_available = True
            try:
                # We load the index (meta + embeddings matrix) only to
                # validate it opens and to read meta. Per-source counts
                # come from _count_sources via a tighter SQL query —
                # cheaper than scanning LoadedIndex.source_paths for
                # duplicates, and it anchors the contract that meta and
                # chunks agree.
                loaded = load_index(index_path)
                report.index_loaded = True
                report.index_meta = loaded.meta
                report.chunk_count = int(loaded.embeddings.shape[0])
                report.sources = _count_sources(index_path)
                report.embedding_model_matches = bool(
                    loaded.meta.embedding_model == cfg.embedding_model
                )
                if not report.embedding_model_matches and loaded.meta.embedding_model:
                    report.issues.append(
                        f"Index was built with {loaded.meta.embedding_model!r} "
                        f"but KB_EMBEDDING_MODEL is {cfg.embedding_model!r}. "
                        "Re-run `kb-ingest` to rebuild with the current model."
                    )
                if report.chunk_count == 0:
                    report.issues.append(
                        "Index loaded but contains zero chunks. "
                        "Re-run `kb-ingest`; check ingest logs for skip warnings."
                    )
            except Exception as exc:  # noqa: BLE001 — SQLite / disk / pickling-style errors
                report.issues.append(
                    f"Index artifact is present but could not be loaded: {exc}. "
                    "The file may be corrupt; re-run `kb-ingest`."
                )

    # Exemplar pool. Optional + graceful — a missing or empty pool is
    # fine, so we never add an "issue" here. The loader is contracted
    # never to raise; we wrap defensively for the same reason
    # ``generate_reply`` does.
    try:
        from wayonagio_email_agent.exemplars import loader as exemplar_loader

        exemplars = exemplar_loader.get_all_exemplars()
        report.exemplar_count = len(exemplars)
        report.exemplar_titles = [ex.title for ex in exemplars]
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders: loader shouldn't raise
        logger.warning(
            "Exemplar loader raised during kb-doctor; continuing with an empty pool: %s",
            exc,
            exc_info=True,
        )

    return report


def format_report(report: DoctorReport, *, max_sources: int = 20) -> str:
    """Render a :class:`DoctorReport` as human-readable text.

    Kept in this module (alongside the data) so tests can assert on the
    exact operator output without depending on Click. ``max_sources``
    truncates the per-source breakdown — the full list is rarely what the
    operator is looking for, and a 200-row dump would bury the important
    fields at the top of the report.
    """
    lines: list[str] = []
    status = "HEALTHY" if report.healthy else "UNHEALTHY"
    lines.append(f"KB status: {status}")
    lines.append("")

    lines.append("Config:")
    lines.append(f"  RAG folders configured:  {report.rag_folder_count}")
    lines.append(f"  Embedding model:         {report.embedding_model}")
    lines.append(f"  Top-K:                   {report.top_k}")
    lines.append(f"  Artifact destination:    {report.artifact_destination}")
    lines.append("")

    lines.append("Index:")
    lines.append(
        f"  Artifact available:      {'yes' if report.artifact_available else 'NO'}"
    )
    lines.append(
        f"  Loaded:                  {'yes' if report.index_loaded else 'NO'}"
    )
    if report.index_meta is not None:
        ingested_at = report.index_meta.ingested_at or "(unknown)"
        age = _ingest_age(report.index_meta.ingested_at)
        age_str = f" ({age})" if age else ""
        lines.append(f"  Ingested at:             {ingested_at}{age_str}")
        lines.append(f"  Chunks:                  {report.chunk_count}")
        lines.append(
            f"  Source files (from meta):{report.index_meta.source_file_count:>4}"
        )
        lines.append(f"  Vector dimension:        {report.index_meta.dimension}")
        match_note = "matches runtime" if report.embedding_model_matches else "MISMATCH"
        lines.append(
            f"  Embedding model in index: {report.index_meta.embedding_model} "
            f"({match_note})"
        )
    lines.append("")

    if report.sources:
        shown = report.sources[:max_sources]
        lines.append(f"Sources indexed ({len(report.sources)}):")
        width = max(len(s.source_path) for s in shown)
        for stat in shown:
            lines.append(
                f"  {stat.source_path.ljust(width)}  {stat.chunk_count:>4} chunks"
            )
        if len(report.sources) > max_sources:
            lines.append(
                f"  ... {len(report.sources) - max_sources} more source(s) not shown"
            )
        lines.append("")

    lines.append("Exemplars:")
    lines.append(f"  Count:                   {report.exemplar_count}")
    if report.exemplar_titles:
        preview = ", ".join(report.exemplar_titles[:5])
        if len(report.exemplar_titles) > 5:
            preview += f", ... ({len(report.exemplar_titles) - 5} more)"
        lines.append(f"  Titles:                  {preview}")
    lines.append("")

    if report.issues:
        lines.append("Issues:")
        for issue in report.issues:
            lines.append(f"  - {issue}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _ingest_age(iso_ts: str) -> str:
    """Return a compact 'Xd Yh ago' for the given ISO timestamp, or ''."""
    if not iso_ts:
        return ""
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    total_hours = int(delta.total_seconds() // 3600)
    if total_hours < 1:
        return "<1h ago"
    days, hours = divmod(total_hours, 24)
    if days:
        return f"{days}d {hours}h ago"
    return f"{hours}h ago"
