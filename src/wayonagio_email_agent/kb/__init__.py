"""Knowledge-base subsystem.

The KB is a **fully optional** feature gated by the ``KB_ENABLED`` env var.
When disabled (the default), the email agent behaves exactly as it did before
this package existed.

When enabled, the KB performs **retrieval-augmented generation** (RAG): content
from the Drive folders listed in ``KB_RAG_FOLDER_IDS`` is chunked, embedded,
and retrieved per email, then inserted into the LLM prompt so replies can cite
agency-specific facts (prices, inclusions, policies) instead of hallucinating.

Retrieval is isolated behind :func:`retrieve.retrieve` so the vector backend
(SQLite + numpy today) can be swapped for a different engine later without
touching the rest of the agent.
"""

from __future__ import annotations

from wayonagio_email_agent.kb import config

__all__ = ["config"]
