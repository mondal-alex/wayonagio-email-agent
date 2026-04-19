"""Exemplars subsystem.

Optional, **graceful** companion to the (required, fail-loud) knowledge base.
Where the KB grounds replies in agency facts, exemplars set the *voice* — the
team's curated example replies, written one Doc per exemplar in a dedicated
Drive folder, raw-injected into the LLM prompt as an ``EXAMPLE RESPONSES``
block.

Design contract (see ``exemplars/README.md`` for the full rationale):

* **One Doc per exemplar.** No chunking, no parsing of headings — each Drive
  Doc is embedded into the prompt as a single unit.
* **Raw injection, not RAG.** At the curator-led pool size we expect
  (10–50 exemplars), the entire pool fits in Gemini's context window and the
  per-request cost of an embedding round-trip plus top-K selection isn't
  worth the operational complexity of a separate ingest pipeline.
* **Cold-start cache.** First call to ``loader.get_all_exemplars()`` reads
  Drive once (in parallel via a thread pool); every subsequent call in the
  same process serves from memory.
* **Optional and graceful.** Every failure path returns ``[]`` and logs at
  WARNING. Exemplars never block a draft — the KB is the only hard
  dependency.
* **Curator-led PII removal.** Operators anonymize while writing the Doc;
  ``sanitize.py`` is a regex tripwire / safety net, not the primary defense.

Public surface used by the rest of the agent:

* ``loader.get_all_exemplars()`` — returns the cached ``list[Exemplar]``.
* ``prompt.format_exemplar_block(exemplars)`` — formats the prompt slot.

The interface boundary at ``format_exemplar_block`` keeps the migration door
open: if the curated pool ever outgrows the context window, we can plug in
embedding + top-K retrieval behind ``loader`` without touching the rest of
the agent.
"""

from __future__ import annotations

from wayonagio_email_agent.exemplars import config

__all__ = ["config"]
