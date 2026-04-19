"""Knowledge-base subsystem.

The KB is a **required** part of the agent. Content from the Drive folders
listed in ``KB_RAG_FOLDER_IDS`` is chunked, embedded, and retrieved per email,
then inserted into the LLM prompt so replies can cite agency-specific facts
(prices, inclusions, policies) instead of hallucinating them.

If the KB is unavailable at draft time (no index ingested, GCS unreachable,
embedding API down, model mismatch), :class:`retrieve.KBUnavailableError` is
raised and the agent refuses to draft — an ungrounded draft that staff might
send unmodified is worse than no draft at all.

Retrieval is isolated behind :func:`retrieve.retrieve` so the vector backend
(SQLite + numpy today) can be swapped for a different engine later without
touching the rest of the agent.
"""

from __future__ import annotations

from wayonagio_email_agent.kb import config

__all__ = ["config"]
