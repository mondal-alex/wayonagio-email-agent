# Knowledge base (`kb/`)

Retrieval-Augmented Generation for the email agent. **Required**
(`KB_RAG_FOLDER_IDS` is mandatory; the agent refuses to draft without a
usable index), **fail-loud** (every failure raises `KBUnavailableError` rather
than silently producing an ungrounded reply), and deliberately small. This
document explains how each piece works, why it looks the way it does, what
it is and isn't good at, and where you'd go next if the agency outgrew it.

---

## What problem this solves

Without the KB, `llm/client.py::generate_reply` asks the LLM to reply to a
client email with no agency-specific context. That works for greetings and
generalities; it hallucinates on specifics ("what's included in the 4-day
Salkantay trek?", "do you offer the Humantay Lake tour in January?").

The KB gives the LLM a small, trustworthy context window of agency prose —
tour descriptions, booking templates, FAQ docs — pulled **per email**, based on
semantic similarity to the client's message. The prompt tells the model:
"for factual questions, only use facts from the reference material; if it's
not in there, don't invent it."

The content lives in **Google Drive** so operators can edit it the same way
they edit any other agency document. An ingest job turns those Drive
files into a compact `kb_index.sqlite` artifact that the API reads at
cold start.

---

## Architecture at a glance

```
┌─────────────────────────────── INGEST (Cloud Run Job, scheduled) ──────────────────────────────┐
│                                                                                                │
│   Drive folders  ──▶  drive.py  ──▶  extract.py  ──▶  chunk.py  ──▶  embed.py  ──▶  store.py   │
│   (KB_RAG_FOLDER_IDS)  list+fetch     PDF/Doc/txt→str   paragraph-aware    LiteLLM        SQLite  │
│                                                         ~800 tok chunks    batched embeddings    │
│                                                                                                │
│                                                              ▼                                 │
│                                                          artifact.py  ──▶  GCS  (kb_index.sqlite)
│                                                                                                │
└────────────────────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────── RUNTIME (Cloud Run API / scanner) ─────────────────────────────┐
│                                                                                                │
│   Client email  ──▶  llm/client.generate_reply  ──▶  retrieve.retrieve(query)                  │
│                                                     │                                          │
│                                                     │ first call:                              │
│                                                     ▼                                          │
│                                     artifact.download_artifact ──▶ /tmp/kb_index.sqlite        │
│                                                     │                                          │
│                                                     ▼                                          │
│                                     store.load_index (slurp into numpy matrix, L2-normalize)   │
│                                                     │                                          │
│                                     subsequent calls: in-memory matmul + top-k                 │
│                                                     │                                          │
│                                                     ▼                                          │
│                            REFERENCE MATERIAL block ──▶ user prompt ──▶ LLM                    │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Ingest is **write-only**. Runtime is **read-only**. They communicate through
a single immutable artifact (`kb_index.sqlite`) published to GCS or a local
directory. This clean split is the most important design decision in the
package; see [Why an artifact boundary?](#why-an-artifact-boundary) below.

---

## Module reference

### `config.py` — env-driven settings

Every KB tunable is resolved from environment variables at the moment
`load()` is called. No import-time caching, no YAML, no Pydantic — a tiny
frozen dataclass whose fields mirror the env vars. Two things worth
highlighting:

- `parse_folder_id()` accepts **both** raw Drive IDs (`1a2b3c…`) and share
  URLs (`https://drive.google.com/drive/folders/1a2b3c…?usp=sharing`). Operators
  paste whatever comes out of the Drive UI; the code normalizes.
- `KB_TOP_K` is clamped to at least 1 and falls back to 4 on garbage input so
  a mistyped env var never takes retrieval silently to zero.

### `drive.py` — Google Drive wrapper

Thin layer over `google-api-python-client`. Three operations:

- `list_folder(folder_id, recursive=True, include_mime_types=…)` — walks
  folders, returns `DriveFile` dataclasses with a human-readable `path` like
  `"Ops / Tour PDFs / Machu Picchu Standard.pdf"` (used as the citation in
  the LLM prompt).
- `read_file(drive_file)` — returns bytes for PDFs and plain text, or the
  plain-text export of a Google Doc.
- `_walk()` — internal recursive helper. Subfolder names come from the same
  `files.list()` response used to find them, so we don't pay a second
  `files.get()` round-trip per subfolder. At 50+ subfolders this matters.

OAuth credentials are shared with `gmail_client` — we added `drive.readonly`
to `SCOPES` so a single `cli auth` flow covers both services. Operators who
authed before the KB feature need to delete `token.json` and re-auth once.

### `extract.py` — text from bytes

Dispatches on MIME type:

| MIME | Strategy |
|---|---|
| `application/pdf` | `pypdf`. Multiple failure modes (encrypted, malformed, `NotImplementedError` on obscure filters) are caught and re-raised as `ExtractionError` so the ingest pipeline can skip-and-log instead of aborting. |
| `application/vnd.google-apps.document` | Trust the Drive export — it's already plain text. |
| `text/plain`, `text/markdown` | UTF-8 decode with `errors="replace"`. |
| Anything else | Raises `ExtractionError`. Ingest swallows it and moves on. |

"Skip and log, don't crash" is the governing principle: a single broken PDF
in a folder of 200 must not take out the whole index.

### `chunk.py` — paragraph-aware splitter

Deterministic, no tokenizer dependency:

1. Normalize line endings, split on blank-line paragraph boundaries.
2. Any paragraph longer than `~800 tokens` gets broken on whitespace.
3. Greedy-pack paragraphs into chunks until the next one would exceed the
   limit. Emit the current chunk with a **100-token tail overlap** so a
   retrieval hit in the middle of a paragraph still sees surrounding context.

Token counting is `chars // 4`, a coarse approximation that's roughly right
for English/Spanish/Italian and completely fine for sizing decisions. We
deliberately avoid `tiktoken` (extra dependency, extra runtime, same effective
chunk size).

### `embed.py` — LiteLLM-backed batch embeddings

Mirrors the design of `llm/client.py` exactly: provider-agnostic via LiteLLM,
provider-specific kwargs only when they apply (`api_base` for Ollama,
`api_key` for Gemini with a clear error if `GEMINI_API_KEY` is missing).

Batches default to **64 texts per request**. Every embedding provider accepts
batched input and it's dramatically cheaper in latency (one RTT per batch) and
in dollars (many providers price per-request rather than per-token).

Defensive response parsing: LiteLLM usually returns an `EmbeddingResponse`
with a `.data` attribute, but some providers wrap it as a plain dict, and a
handful expose entries as objects (`.embedding`) vs dicts (`["embedding"]`).
The code handles all shapes and **refuses to persist empty vectors** — an
empty vector in the index would silently poison every future retrieval.

### `store.py` — SQLite vector store

One SQLite file, two tables:

- `chunks`: text, Drive metadata, embedding as a `BLOB` (float32, little-endian).
- `meta`: key/value pairs (embedding model, dimension, ingest timestamp,
  source file count, chunk count). Written once per ingest, consulted at load.

**Search doesn't happen in SQL.** At load time we slurp the whole `chunks`
table into one numpy matrix, L2-normalize it once, and hand it back as a
`LoadedIndex`. Retrieval is a single matmul + `argpartition` top-k:

```python
scores = self.embeddings @ query_unit  # (n, d) × (d,) → (n,)
top_idx = np.argpartition(-scores, k)[:k]
```

Exact cosine similarity (not approximate), because pre-normalized dot
product _is_ cosine. At the agency's scale (~thousands of chunks) this is
sub-millisecond on a Cloud Run CPU and beats every networked vector DB on
latency — see [Why SQLite + numpy?](#why-sqlite--numpy) below.

Write semantics are **atomic-replace**: we delete the existing file and
write a fresh one. No partial updates, no rows-half-migrated states. The
artifact is either the old valid index or the new valid index, never a
frankenstein of both.

### `artifact.py` — GCS + local artifact I/O

One module knows how to locate an artifact. The ingest Job calls
`upload_artifact(cfg, local_path, filename)`; the runtime calls
`download_artifact(cfg, filename, cache_dir)`. Both transparently switch
between:

- **GCS** (production) — when `KB_GCS_URI=gs://bucket[/prefix]` is set.
- **Local filesystem** (dev / single-host / tests) — falls back to
  `KB_LOCAL_DIR`, default `./kb_artifacts/`.

The `google.cloud.storage` import is **function-local** so the runtime path
(which only reads) doesn't pay the import cost just to touch the module. Real
GCS errors at download time are caught and logged at ERROR level, then we
return `None` so retrieval degrades instead of crashing.

### `retrieve.py` — runtime-side KB access

The only module `llm/client.py` imports from. Responsible for:

1. **Lazy loading.** First call downloads the artifact into a process-local
   cache (`/tmp/wayonagio_kb_cache/`), loads it into memory, and pins it.
2. **Thread-safe cache.** Module-level lock + double-checked lock inside
   `_ensure_loaded()` — safe under Uvicorn's async event loop because
   FastAPI's sync-endpoint path runs handlers on a threadpool.
3. **Fail-loud, never silent.** Every failure mode (artifact missing,
   embedding error, empty index, model mismatch) raises `KBUnavailableError`
   so `generate_reply` aborts the draft. Refusing to draft is preferable to
   producing an ungrounded reply that staff might send unmodified.
4. **Failed loads are not cached.** If `_load_state` raises, the global
   `_state` stays `None`, so a transient outage (GCS hiccup, race with
   in-progress ingest) self-heals on the next request.
5. **Embedding-model mismatch detection.** If the loaded index was built with
   a different `embedding_model` than the current `KBConfig`, we raise with
   a clear "re-run kb-ingest" message — a mismatched model means mismatched
   vector dimensions, which would otherwise crash the matmul on every draft.
6. **`reset_cache()`** — exposed for tests, and reserved for a future admin
   `POST /kb/reload` endpoint if we ever want to hot-swap the index without
   a redeploy.

### `ingest.py` — the pipeline orchestrator

A straight-line function: resolve config → list Drive folders → extract +
chunk + embed → write SQLite → upload. Single-pass, stateless, idempotent.
Two safety guards worth calling out:

- **Requires `KB_RAG_FOLDER_IDS`** — `config.load()` raises if it isn't set,
  so misconfiguration is caught at startup rather than silently producing
  no-op artifacts.
- **Refuses to publish an empty index when RAG folders are configured** — if
  every file failed to extract (perms, corrupt PDFs, empty folder), aborting
  is far better than silently overwriting a previously-good index with a
  zero-row one (which would in turn poison every subsequent draft).

---

## Runtime behaviour (what actually happens per email)

1. FastAPI receives `POST /draft-reply` → `agent.manual_draft_flow` → `llm/client.generate_reply`.
2. `generate_reply` calls `kb_retrieve.retrieve(original_email_text)`.
3. `retrieve()` on first use:
   - Loads `KBConfig` from env. Raises `KBConfigError` if `KB_RAG_FOLDER_IDS`
     is missing.
   - Downloads `kb_index.sqlite` from GCS/local into `/tmp`. Raises
     `KBUnavailableError` if the artifact is missing, the index is empty, or
     the embedding model has been rotated without a re-ingest.
   - Slurps it into a numpy matrix, L2-normalizes once.
   - Caches the `LoadedIndex` under a lock. Failed loads are NOT cached, so
     transient outages self-heal on the next request.
4. `retrieve()` on every use:
   - Embeds the query via LiteLLM (single text, single HTTP call — this is
     the only per-draft network round-trip the KB adds). Provider errors
     propagate.
   - Matmul + `argpartition` → top-K `ScoredChunk`s.
5. `generate_reply` formats the hits as a `--- REFERENCE MATERIAL ---` block
   and appends them to the **user prompt** (not the system prompt, so they
   can be adjusted per email).
6. LLM generates the draft; Gmail API creates it. The client sees a draft
   grounded in real agency facts.

If any step from 3 onward fails, `generate_reply` re-raises the exception and
no draft is created. The caller (FastAPI handler or scanner loop) surfaces
the error rather than producing a hallucinated reply.

**Cost per reply** (Gemini, default settings, typical small corpus):

- 1 embedding call, ~200 tokens → free tier.
- 1 chat completion call, ~3k input tokens (prompt + email + 4 chunks) →
  fractions of a cent.

At 100 replies/day the monthly KB cost is dominated by GCS storage — a few
cents for the index file. This is on purpose.

---

## Design decisions, explained

### Why an artifact boundary?

Ingest reads Drive (slow, heavy deps, needs Drive OAuth) and writes vectors
(CPU-heavy, needs `pypdf`, needs embedding API access). Runtime reads
vectors (fast, single query). Mashing both into the same process would mean:

- Every API container pulling PDF dependencies it never uses.
- Every API cold start re-walking Drive.
- Re-ingest requiring an API redeploy.
- No clear "known-good vs being-rebuilt" boundary.

The `kb_index.sqlite` artifact is a **hard seam** between the two. Ingest
publishes a new file atomically; runtime picks it up on the next cold start
(or on an explicit `reset_cache()`). You can rebuild the KB without
touching the API at all, and the API has exactly one dependency on ingest:
"a file at a known URI." That file is byte-identical across hosts, trivially
cacheable, trivially rollbackable (keep the old file, change the URI), and
trivially inspectable (`sqlite3 kb_index.sqlite "SELECT source_path, length(text) FROM chunks;"`).

### Why Google Drive as the source of truth?

The agency already edits tour descriptions, templates, and FAQ docs in
Drive. Any other answer — Airtable, Notion, a CMS, a git repo of Markdown —
would be "please change your workflow for our tool's convenience." The code
adapts to the business, not the other way round. `KB_RAG_FOLDER_IDS` accepts
whatever folder structure already exists.

### Why a yearly re-ingest cadence is fine

This corpus is edited **roughly once a year** — the seasonal refresh of
tour catalogs, prices, and policies. That single fact removes most of the
operational anxiety usually associated with a RAG system:

- **Staleness risk is tiny.** "When was the index built?" has a boring
  answer for 11 months out of 12. We don't need a streaming pipeline, a
  change-data-capture watcher on Drive, or a cache TTL — re-ingest on
  the calendar matches how the data actually changes.
- **Cost is a once-a-year line item.** The embedding API is the only
  recurring cost the KB introduces, and "once a year × a few thousand
  chunks" is rounding-error money. We don't need an embedding cache or
  incremental ingest.
- **Operational responsibility flips.** Instead of "is the daily ingest
  Job healthy?" the operator question becomes "did this year's refresh
  run after the seasonal Drive edits landed?" — a single calendar
  reminder, not a continuous monitoring concern.

The trade-off: **the yearly run must actually happen.** A scheduled
Cloud Run Job (see the README) handles this on autopilot, and any
out-of-cycle edit in Drive should trigger an on-demand `cli kb-ingest`
so the next draft sees the change without waiting for the next yearly
window.

### Why SQLite + numpy?

This is the decision the wider ecosystem pushes back on hardest, so it's
worth a full explanation.

We are **not doing vector search in SQLite**. SQLite is the shipping
format — a single-file, zero-ops, byte-identical-across-hosts way to publish
embeddings alongside their text. All similarity search happens in numpy, in
memory, after `load_index` has slurped the whole table.

At the target scale:

- Corpus: a few hundred to a few thousand chunks.
- Embedding dim: 3072 (Gemini `gemini-embedding-001`).
- Memory: ~40 MB per 3,000 chunks (3072 float32 × 3 000 ≈ 36 MB, plus text).
- Search: `(n, d) × (d,)` matmul + O(n) top-k. Sub-millisecond.

Versus the alternatives:

| Option | Pros | Cons vs us at this scale |
|---|---|---|
| **pgvector** | Perfect when Postgres is already in the stack | Extra infra; network RTT dwarfs local compute at our size |
| **Pinecone / Weaviate / Qdrant** | Scale to 10M+ vectors; real ANN indexes | Extra vendor, extra money, extra latency; we don't need ANN below ~50k vectors |
| **ChromaDB** | SQLite + `hnswlib` ANN; great DX | Heavier dep tree; ANN pays off only above our ceiling |
| **LanceDB** | Columnar + vector; embedded | Newer, smaller community |
| **FAISS raw** | Fastest in-process | Index lifecycle + persistence you'd have to write anyway |

Our approach is the well-understood "serverless vector search" pattern:
ship a flat matrix alongside the container, search in RAM, skip the
network hop entirely. When we outgrow it, we swap the backend behind the
`store.LoadedIndex` / `retrieve.retrieve()` interface — a one-file change.

### Why one artifact, not per-source files?

Early sketches had one file per Drive source. Concatenating at load time
felt right until the thread-safety question came up: "what if ingest
half-uploads the new set?" With one file, atomic replace is trivial.
With N files, you'd need a manifest, a generation ID, and a reconciliation
loop at load. Not worth the complexity at our size.

### Why LiteLLM for embeddings too, not just chat?

Same reason as `llm/client.py`: provider portability without code changes.
`KB_EMBEDDING_MODEL=gemini/gemini-embedding-001` today, `ollama/nomic-embed-text`
in a disconnected-lab scenario, `openai/text-embedding-3-small` if we ever
switch providers for unrelated reasons. The config swap is the deploy.

### Why paragraph-aware chunking, not fixed-size?

Retrieval quality is highly sensitive to chunk boundaries. Cutting mid-
sentence produces fragments that answer nothing. Cutting at paragraph
boundaries preserves the author's logical units, and the 100-token overlap
means a query matching the middle of a paragraph still gets surrounding
context. The `chars // 4` token heuristic is coarse but retrieval doesn't
need tokenizer precision — it needs chunks that are "roughly right."

### Why fail fast, not fail soft, at runtime?

Earlier iterations of this module degraded silently to the base prompt on
KB failure. We changed that, because the agent's value proposition **is**
grounded drafting — without RAG it's "any LLM with a Gmail token," which is
not what the agency wants in front of customers. An ungrounded draft that
staff send unmodified is a strictly worse outcome than no draft at all,
because at least "no draft" tells staff to write the reply themselves.

Every KB error path now raises `KBUnavailableError` (or propagates the
underlying provider exception). The caller — `generate_reply`, then the
FastAPI handler or scanner loop — surfaces the error. Operators see a clear
"KB unavailable, run kb-ingest" message in logs and the response, fix the
underlying issue, and the next request succeeds. The draft-only invariant is
still sacred — refusing to draft does not violate it; producing an
ungrounded one nearly does.

---

## Advantages

- **Zero infra beyond a GCS bucket.** No vector DB, no container to babysit,
  no extension to install. A SQLite file and an object store.
- **Fast retrieval.** No network hop for the similarity search itself.
  Faster than pgvector / Pinecone / Weaviate at our corpus size.
- **Cheap.** Embedding calls are the only recurring cost, and Gemini's free
  tier covers typical volumes. GCS storage is cents per month.
- **Atomic rebuilds.** One file swap = one KB update. Rollbacks are a
  gcloud one-liner (`gsutil cp gs://…/kb_index.sqlite.20260410 gs://…/kb_index.sqlite`).
- **Fails loud.** A misconfigured or stale KB cannot silently degrade
  drafting quality — the API returns an error and operators get told to fix
  it before any unreliable drafts can leak to customers.
- **Drive-native.** Operators keep editing the same documents they always
  did. No migration, no parallel copy, no training.
- **Inspectable.** `sqlite3 kb_index.sqlite` gives you a shell into the
  corpus. Every CI box, every laptop has the tool.
- **Deterministic.** Same inputs → same artifact. Trivial to diff two
  ingests when retrieval quality regresses.

---

## Limitations

- **No metadata filtering at query time.** We don't have `WHERE`-clauses
  on retrieval; every chunk is equally eligible. If you needed "similar
  chunks, but only from the 2026 tour catalog," the current design falls
  over. Would require either (a) multiple indexes and a router, or (b) a
  move to pgvector / sqlite-vec.
- **Exact (brute-force) search only.** Linear in corpus size. Fine below
  ~50,000 chunks, noticeably slow above ~100,000. We are nowhere near
  this ceiling.
- **Full-memory load.** Every API container holds a full copy of the
  matrix. At 10 MB this is free; at 1 GB it hurts cold start and bloats
  per-replica memory.
- **Cold-start download.** First draft after a Cloud Run cold start pays
  the artifact download (~tens to low hundreds of milliseconds from the
  same region). We could precompute a `/tmp` cache in the container image
  if this ever mattered.
- **Re-embed on every ingest.** No incremental embedding cache. A full
  rebuild hits the embedding API once per chunk every time. At a few
  thousand chunks this is ~a few cents; at 100,000 it becomes worth
  deduping on content hash.
- **English-centric token heuristic.** `chars // 4` is roughly right for
  European languages. Chinese/Japanese/Korean content would be under-
  counted and chunks would be oversized. Not a real concern for a Cusco
  travel agency but worth flagging.
- **One embedding model at a time.** `retrieve.py` refuses to use an
  index built with a different model. Rotating models requires re-ingest
  before a runtime config change takes effect.
- **No hybrid search (BM25 + vectors).** Pure semantic retrieval misses
  exact-string queries (SKUs, dates, specific tour codes) the way keyword
  search would catch them. Can be bolted on later.

---

## Upgrade paths

Phased so each step keeps the current interface intact. The seam is
`retrieve.retrieve()` returning a `list[ScoredChunk]`, and `store.LoadedIndex`
as the abstract "vector store" object. Anything can go behind them.

### Step 1 — `sqlite-vec` extension

**Trigger**: corpus pushes past ~50k chunks, or we want metadata filters.

Load the `sqlite-vec` extension at ingest and runtime; replace the in-memory
matmul with a `SELECT … FROM vec_index WHERE …` query. Keep the single-file
deployment model, gain a real KNN index and SQL-native filtering. Smallest
possible jump. ~100 lines of change inside `store.py`.

### Step 2 — pgvector

**Trigger**: the app grows a real Postgres database (bookings, guest CRM,
tour inventory).

Colocate vectors with business data. `KBConfig` gains a `database_url`;
`ingest.py` writes to Postgres instead of SQLite; `store.py` becomes a thin
pgvector client. One backup story, one backup tool, one place to check.

### Step 3 — dedicated vector DB (Qdrant / Weaviate / Pinecone)

**Trigger**: multi-region deployment, ~1M+ vectors, or latency SLOs tight
enough that the artifact download at cold start is measurable.

Index lifecycle gets managed by the vendor, ANN becomes the default, and
scaling becomes their problem. The code change is localized to `store.py`
and `retrieve.py`; the prompt-construction side of the KB never notices.

### Orthogonal improvements (worth doing before step 2)

- **Incremental ingest** — cache embeddings by content hash; skip unchanged
  chunks on re-ingest. Cuts ingest cost to near zero when Drive content
  stabilizes.
- **Hybrid retrieval** — add a cheap BM25 pass (via `rank-bm25` or SQLite
  FTS5), blend with cosine scores. Fixes exact-string queries without
  abandoning semantic search.
- **Context caching** — Gemini's context-caching API lets us cache the
  long, stable parts of the prompt. The reference material changes per
  email but the system prompt doesn't.
- **`/admin/kb/reload` endpoint** — call `retrieve.reset_cache()` without
  a redeploy. `reset_cache()` already exists; just needs an auth'd route.
- **Retrieval quality telemetry** — log `source_path` + score for every
  retrieval. Makes "why did it answer X" debuggable after the fact.

---

## Operational playbook

**Refresh cadence.** The Drive material is edited **roughly once a year**
(seasonal refresh of tour catalogs, prices, and policies). Schedule the
ingest Job accordingly — a yearly Cloud Scheduler run after the agency's
annual content review is sufficient, with on-demand `cli kb-ingest` runs
for any out-of-cycle edits. Set a real calendar reminder for the yearly
run; "we forgot" is the only realistic way this corpus goes stale.

**Add content**: drop it in one of the `KB_RAG_FOLDER_IDS` folders in Drive.
Trigger ingest (manually with `cli kb-ingest`, or wait for the next yearly
Cloud Run Job if the change can wait). Artifact re-uploads atomically. Next
draft sees the new content after the next cold start, or on demand via a
future `/admin/kb/reload` endpoint.

**Remove content**: delete the file in Drive. Re-ingest. Gone.

**Rotate embedding model**: change `KB_EMBEDDING_MODEL`. **Re-ingest first**,
then update the runtime env. Between those two steps, runtime will detect
the mismatch and refuse to draft with a loud `KBUnavailableError` — you do
not get a subtle retrieval-quality regression, you get an explicit "the
index doesn't match the configured model; re-run kb-ingest before any drafts
will succeed."

**Debug retrieval**: `uv run python -m wayonagio_email_agent.cli kb-search
"the exact email text"` prints the top-K hits with scores and source paths.
If the expected chunk isn't there, the problem is in ingest (missing file,
failed extraction) or chunking (was it split across two chunks such that
neither matched?). If the expected chunk is there but the reply still
hallucinates, the problem is in the prompt, not the retrieval.

**Inspect the index**: `sqlite3 /path/to/kb_index.sqlite "SELECT
source_path, chunk_index, length(text) FROM chunks ORDER BY
source_path;"`. All the usual SQLite tooling just works.

**Roll back**: keep a versioned copy of `kb_index.sqlite` in GCS before
each ingest (e.g. `kb_index.sqlite.20260417-1203`). Point `KB_GCS_URI` at
the old file; call `/admin/kb/reload` or redeploy. Instant rollback.

---

## Testing

Every module has unit tests in `tests/test_kb_*.py`. The high-signal ones:

- `test_kb_store.py` — round-trips write/load, confirms L2 normalization
  and `top_k` ordering.
- `test_kb_ingest.py` — end-to-end with mocked Drive + mocked embeddings;
  covers the empty-RAG-index safety guard and the skip-on-extract-error
  path.
- `test_kb_retrieve.py` — thread-safe cache, fail-loud behavior on missing
  artifact / embedding errors / model mismatch, and the no-poison guarantee
  that failed loads aren't cached.
- `test_llm.py::TestGenerateReplyKBIntegration` — wire-up tests that the
  reference block lands in the user prompt when hits exist, and that KB
  failures abort drafting (rather than silently producing an ungrounded
  reply).

The KB is designed for this test style: each module is import-cheap,
does one thing, and has no hidden I/O on the happy path.
