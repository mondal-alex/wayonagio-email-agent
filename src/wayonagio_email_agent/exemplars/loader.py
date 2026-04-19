"""Process-level cache for exemplars.

The runtime contract is intentionally simple:

* First call to :func:`get_all_exemplars` triggers Drive collection (in
  parallel — see ``source.collect``), sanitization, and caching. Every
  subsequent call in the same process returns the cached list verbatim,
  microseconds-fast.
* Any failure during the cold-start load (Drive unreachable, no folder
  configured, every Doc broken) is *captured* — the cache is set to ``[]``
  and a WARNING is logged. We deliberately do **not** retry on every
  request: if Drive is down, repeatedly hammering it would waste quota and
  could turn a transient outage into a sustained one. Cloud Run cycles
  instances frequently enough that natural refresh on the next cold start
  is sufficient. (For an immediate refresh, the operator triggers a new
  Cloud Run revision rollout.)
* :func:`get_all_exemplars` **never raises**. The whole point of the
  exemplars subsystem is to be optional and graceful — an empty list is a
  valid steady state and is what callers see whenever the feature is
  disabled, the load failed, or the folder is genuinely empty. The caller
  treats all three identically.

The cache is guarded by a double-checked :class:`threading.Lock` so the
FastAPI threadpool never builds the index twice under concurrent first-
hit requests. The same primitive is used by :mod:`kb.retrieve` for the
same reason.

:func:`reset` is exposed for tests only. There is intentionally no public
"reload" API on the agent — refreshing exemplars in production is done by
restarting the process (Cloud Run revision rollout). A future
``POST /admin/exemplars/reload`` endpoint would call :func:`reset`
followed by :func:`get_all_exemplars`.
"""

from __future__ import annotations

import logging
import threading

from wayonagio_email_agent.exemplars import config as exemplar_config
from wayonagio_email_agent.exemplars import source as exemplar_source
from wayonagio_email_agent.exemplars.source import Exemplar

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: list[Exemplar] | None = None


def get_all_exemplars() -> list[Exemplar]:
    """Return the cached list of exemplars, populating it on first call.

    Never raises. Returns ``[]`` when:

    * exemplars are disabled (``KB_EXEMPLAR_FOLDER_IDS`` unset), or
    * the previous cold-start load failed (cached ``[]``), or
    * the configured Drive folders are genuinely empty.
    """
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            cfg = exemplar_config.load()
            if not cfg.enabled:
                logger.info(
                    "Exemplars disabled (KB_EXEMPLAR_FOLDER_IDS unset); "
                    "drafts will not include an EXAMPLE RESPONSES block."
                )
                _cache = []
            else:
                exemplars = exemplar_source.collect(cfg)
                logger.info(
                    "Loaded %d exemplar Doc(s) from Drive.", len(exemplars)
                )
                _cache = exemplars
        except Exception as exc:  # noqa: BLE001 — we promise never to raise
            logger.warning(
                "Exemplar load failed; continuing without exemplars for the "
                "lifetime of this process: %s",
                exc,
            )
            _cache = []
        return _cache


def reset() -> None:
    """Clear the cache so the next :func:`get_all_exemplars` call re-loads.

    **Test-only.** Production callers must not invoke this — restarting the
    process (Cloud Run revision rollout) is the supported refresh
    mechanism, and that path also reloads ``.env`` and any rotated
    credentials, which a bare ``reset()`` would not.
    """
    global _cache
    with _lock:
        _cache = None


__all__ = ["Exemplar", "get_all_exemplars", "reset"]
