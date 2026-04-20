# Architecture

Technical writeup of how this codebase is wired together and *why* it looks
the way it does. Companion to:

- [`README.md`](README.md) — user/operator setup, deployment, and security posture.
- [`AGENTS.md`](AGENTS.md) — quick orientation for AI coding agents.
- [`src/wayonagio_email_agent/kb/README.md`](src/wayonagio_email_agent/kb/README.md) — deep dive on the knowledge base module specifically.
- [`src/wayonagio_email_agent/exemplars/README.md`](src/wayonagio_email_agent/exemplars/README.md) — deep dive on the exemplars module specifically.

If a topic is well-covered in one of those, this document refers out instead
of duplicating. The goal here is the *system-level* story: who calls whom,
what the contracts are, and what design constraints shaped the code.

---

## 1. Mental model

At its core the agent is a thin chain between **three external systems** and
**one internal artifact**:

```
┌──────────┐       ┌─────────────┐       ┌──────────┐
│  Gmail   │  ◀──▶ │   AGENT     │  ◀──▶ │   LLM    │
│  (HTTP)  │       │  (Python)   │       │ (HTTP)   │
└──────────┘       └─────┬───────┘       └──────────┘
                         │
                         ▼
                  ┌────────────────┐
                  │ kb_index.sqlite│  ← built offline from Google Drive
                  │ (file in GCS)  │
                  └────────────────┘
```

That picture is the whole product. Every Python module exists to serve one
of these three boundaries:

| Boundary | Owned by | Notes |
|---|---|---|
| Gmail (read messages, create drafts) | `gmail_client.py` | OAuth2; **`gmail.send` scope is intentionally absent**. |
| LLM (chat completions, embeddings) | `llm/client.py`, `kb/embed.py` | Both go through LiteLLM so the provider is a config knob. |
| KB index (`kb_index.sqlite` in GCS) | `kb/` (split read/write) | Built by an offline ingest job; read by the runtime. |
| Exemplars (curator-led example replies in Drive) | `exemplars/` | Optional, raw-injected at prompt time, cached per process. |

Three entry points sit on top:

- **`api.py`** — `POST /draft-reply` for the Gmail Add-on (synchronous, low-volume).
- **`cli.py`** — admin commands (`auth`, `list`, `draft-reply`, `scan`, `scan-once`, `kb-ingest`, `kb-search`, `kb-doctor`, `exemplar-list`).
- **`agent.py`** — orchestration shared by both: `manual_draft_flow`, `scan_once`, `scan_loop`.

There is no web UI, no database server (just a file), and no message queue.
That's deliberate; see [§7 Why this shape](#7-why-this-shape).

---

## 2. The three flows

### 2.1 Manual draft (Gmail Add-on or `cli draft-reply`)

The hot path. Triggered every time a staff member clicks "Draft in Italian"
on an open Gmail thread.

```
Gmail Add-on (Apps Script)
    │
    │  POST /draft-reply  { message_id, language? }
    │  Authorization: Bearer <AUTH_BEARER_TOKEN>
    ▼
api._verify_token  ─── HMAC-compare bearer (constant time) ───▶ 401 on mismatch
    │
    ▼
api.draft_reply  (sync def — runs in FastAPI threadpool, see §5.3)
    │
    ▼
agent.manual_draft_flow(message_id, forced_language)
    │
    ├──▶ gmail_client.get_message(message_id)        ── Gmail API
    ├──▶ gmail_client.extract_message_parts(...)     ── parse MIME tree
    ├──▶ llm.detect_language(body)                   ── only if no forced_language
    ├──▶ llm.generate_reply(original, language)
    │       └──▶ kb.retrieve.retrieve(original)      ── REQUIRED, raises if KB down
    │       └──▶ kb.retrieve.format_reference_block  ── prepend to user prompt
    │       └──▶ exemplars.loader.get_all_exemplars  ── OPTIONAL, never raises (cached)
    │       └──▶ exemplars.prompt.format_exemplar_block  ── append AFTER reference block
    │       └──▶ litellm.completion(...)             ── LLM call
    │       └──▶ EmptyReplyError if blank
    └──▶ gmail_client.draft_reply(...)               ── drafts.create ONLY
```

**Prompt block ordering is load-bearing.** The exemplar block's framing
("the REFERENCE MATERIAL above is authoritative") only reads correctly if
the KB block actually appears above it on the page. `llm/client.py`
explicitly comments this; reordering the two blocks without updating
`exemplars/prompt.py`'s wording would silently change the semantics
(exemplars could start appearing as fact sources, defeating the
KB-required invariant).

**Exemplars are also gated on a non-empty reference block.** Two reasons:
the framing literally references "REFERENCE MATERIAL above" (incoherent
without one), and exemplars without KB grounding risk the model copying
example facts unmoored from any canonical source. In practice the KB
nearly always returns hits, so this branch fires almost always; the
guard exists for the pathological edge cases (`top_k=0`, an empty
index) where the prompt would otherwise self-contradict.

What can go wrong, and how the API surfaces it:

| Failure | Maps to | Detail header |
|---|---|---|
| Bearer missing/wrong | 401 | `Invalid bearer token.` |
| `AUTH_BEARER_TOKEN` unset on server | 500 | `Server authentication is not configured.` |
| `KBUnavailableError` / `KBConfigError` | **503** | `Knowledge base unavailable. Ask the operator to run kb-ingest.` |
| `EmptyReplyError` from LLM | **502** | `LLM returned an empty reply. Please retry.` |
| Gmail OAuth refresh fails (`SystemExit`) | 503 | `Gmail authentication failed. Run cli auth on the server.` |
| Anything else unexpected | 500 | Generic detail; full traceback in server logs only. |

The 502/503 split is intentional: 503 says "our dependency is down, fix the
server", 502 says "an upstream returned garbage, retrying may work".

### 2.2 Automatic scanner (`cli scan-once` or `cli scan`)

Runs on a schedule (Cloud Scheduler → Cloud Run Job for the agency
deployment, or systemd / cron for self-hosted). Two entry points wrap the
same `_process_message` logic:

- `scan_once(dry_run)` — one pass, returns. Used by external schedulers.
- `scan_loop(interval, dry_run)` — `while True` loop. Used in always-on hosts.

Per-message logic in `agent._process_message`:

```
1. state.is_processed(message_id)?              ── primary dedup, SQLite
   └─▶ skip
2. gmail_client.get_message + extract_message_parts
3. llm.is_travel_related(subject, body)         ── tiny yes/no + lang prompt
   └─▶ if no: state.mark_processed(outcome="non_travel")
4. gmail_client.thread_has_draft(thread_id)     ── secondary dedup, Gmail API
   └─▶ if yes: state.mark_processed(outcome="thread_has_draft")
5. llm.generate_reply(...)                      ── same path as manual
6. gmail_client.draft_reply(...)                ── drafts.create
7. state.mark_processed(outcome="drafted")
```

Two layers of dedup are deliberate: SQLite is fast and prevents repeated LLM
spend; the Gmail-thread check catches the case where another process (a
staff member, or a parallel scanner instance) already drafted into the
thread. State writes happen **after** the side effect, so a crash mid-draft
just causes a retry on the next pass — never a silent skip.

`scan_once` catches per-message exceptions and continues; one bad message
never aborts the batch. `scan_loop` additionally catches per-iteration
exceptions and just sleeps until the next interval — there is no global
abort.

### 2.3 KB ingest (`cli kb-ingest`, scheduled yearly)

Offline pipeline. Owned by `kb/ingest.py`, which strings together the rest
of the `kb/` module:

```
config.load()
    │
    ▼
for folder_id in KB_RAG_FOLDER_IDS:
    drive.list_folder(folder_id, recursive=True)
        │
        ▼
    for drive_file in files:
        drive.read_file(drive_file)         ── bytes (or str for Google Docs)
        extract.extract_text(...)           ── PDF / Doc / txt / md → str
        chunk.chunk_text(...)               ── ~800-token paragraphs w/ overlap

embed.embed_texts(all_chunks, model=KB_EMBEDDING_MODEL)
    └─▶ batched LiteLLM embedding calls

if rag_sources == 0 or not chunks or embeddings.size == 0:
    raise RuntimeError                      ── refuse to publish empty index

store.write_index(tmp_path, ...)            ── SQLite blob with embeddings
artifact.upload_artifact(...)               ── GCS or KB_LOCAL_DIR
```

Why ingest and runtime live in the **same package** but communicate **only
through the artifact**: see [`kb/README.md` §"Why an artifact boundary?"](src/wayonagio_email_agent/kb/README.md).
Short version: write-once-read-many, atomic deploy, runtime never touches
Drive.

---

## 3. Module reference

For each module: what it owns, what it explicitly does not, and the contract
it presents to the rest of the codebase.

### `gmail_client.py`

**Owns:** all Gmail and Drive API access, OAuth credential lifecycle, and
MIME assembly for outgoing drafts.

**Does not own:** any retrieval logic, any LLM logic, any state. It returns
plain dicts; orchestration lives in `agent.py`.

**Public contract:**

| Function | Returns | Notes |
|---|---|---|
| `load_credentials()` | `Credentials` | Refreshes silently; raises `SystemExit(1)` on revocation. |
| `run_auth_flow()` | `Credentials` | Interactive — needs a browser. Used by `cli auth`. |
| `list_messages(q, max_results)` | `list[dict]` | Each dict has just `id` and `threadId`. |
| `get_message(message_id)` | `dict` | Full Gmail payload. |
| `get_messages_metadata(ids, headers)` | `list[dict]` | Single batched request — N+1 avoidance for `cli list`. |
| `thread_has_draft(thread_id)` | `bool` | Secondary scanner dedup. |
| `draft_reply(...)` | `dict` | **Only ever calls `drafts.create`.** Never `drafts.send` or `messages.send`. Asserted by `TestDraftOnlyInvariant`. |
| `extract_message_parts(message)` | `dict` | Parse helper. Recursive `text/plain` walk; ignores attachments. |

**Invariant:** `gmail_client` never sends mail. The OAuth scopes are
`gmail.readonly + gmail.compose + drive.readonly` only. `gmail.send` is
**physically absent** from `SCOPES`, so the server cannot send even if
compromised. This is the most important defensive property in the codebase.

### `llm/client.py`

**Owns:** every LLM chat completion in the system. Provider abstraction via
LiteLLM. Prompt templates for the three tasks.

**Does not own:** embeddings (those live in `kb/embed.py`), retrieval, or
any Gmail concept.

**Public contract:**

| Function | Purpose | Failure mode |
|---|---|---|
| `detect_language(text)` | Returns `"it"` / `"es"` / `"en"`. | Defaults to `"en"` on unparseable response (logged). |
| `generate_reply(original, language)` | Builds the grounded prompt, calls LiteLLM. | Raises `EmptyReplyError` on blank reply. Propagates `KBUnavailableError`. |
| `is_travel_related(subject, body)` | Returns `(bool, lang)`. Used by scanner only. | Defaults to `(False, "en")` on garbage. |

**Invariant:** `generate_reply` always retrieves from the KB and always
threads the `REFERENCE MATERIAL` block into the user prompt before calling
the LLM. There is no ungrounded path — by design, after the KB-required
pivot. The prompt explicitly tells the model: *"only use facts that appear
in the reference material; if not present, ask for clarification."*

**Why LiteLLM:** swapping providers (Gemini ↔ Ollama ↔ OpenAI ↔ Anthropic
↔ Vertex) is a single env var. No `if provider == "gemini": ... elif
provider == "ollama": ...` branches in business logic. Provider-specific
knobs (Ollama's `keep_alive`, Gemini's `api_key`) are confined to
`_build_kwargs`.

### `agent.py`

**Owns:** orchestration. Glue between `gmail_client`, `llm.client`, `state`,
and (transitively, via the LLM client) `kb`.

**Does not own:** any I/O of its own. Tested with stubs for everything
underneath; 100% coverage.

**Public contract:**

| Function | Used by |
|---|---|
| `manual_draft_flow(message_id, forced_language=None)` | `api.draft_reply`, `cli draft-reply`. |
| `scan_once(dry_run)` | `cli scan-once`, Cloud Run Jobs. |
| `scan_loop(interval, dry_run)` | `cli scan`, systemd unit. |
| `scanner_enabled()` | Both `cli scan` commands gate on this. |

**Header handling note:** `_build_references` collapses internal whitespace
when appending to an existing `References` chain. Real-world headers occasionally
arrive with multiple spaces (line-folded headers, upstream reformatting), and
emitting them verbatim produces ugly `References:` lines in some MUAs.

### `api.py`

**Owns:** the FastAPI app, bearer-token auth, two middlewares (body-size
cap, security headers), and the global exception handler.

**Does not own:** any drafting logic. The two route handlers are eight lines
combined.

Routes:

- `GET /healthz` — unauthenticated liveness probe. Does **not** call Gmail
  or the LLM — Cloud Run health checks must not depend on external services.
- `POST /draft-reply` — bearer-protected; thin wrapper around
  `agent.manual_draft_flow` with the error mapping shown in §2.1.

**Critical detail:** `draft_reply` is a **sync `def`**, not `async def`.
`manual_draft_flow` does multi-second blocking I/O (Gmail HTTPS, LLM
completion, SQLite, optional GCS download). Inside an async handler that
would block the entire event loop and freeze every concurrent request
including `/healthz`. As a sync handler, FastAPI runs it in its threadpool
and concurrent requests parallelize. This is documented inline because it's
counter-intuitive to readers who assume "async = faster".

**Middleware ordering matters.** `add_middleware` is LIFO — the **last**
middleware added runs **first** (outermost). `_SecurityHeadersMiddleware` is
added last so it wraps `_BodySizeLimitMiddleware`; otherwise an attacker who
trips the 413 limit would get a response with no HSTS / X-Frame-Options
headers. Defense in depth.

### `cli.py`

**Owns:** the user-facing admin surface. Click-based; each subcommand is a
thin wrapper around `agent.*` or `kb.*`.

**Does not own:** any business logic. The translation layer between
runtime exceptions and clean operator output lives here:

- `KBUnavailableError` / `KBConfigError` → `click.ClickException` (no traceback).
- `EmptyReplyError` → `click.ClickException` (no traceback).
- `SCANNER_ENABLED=false` → `click.ClickException` from both scan commands.

This mirrors the API's status-code mapping. An operator running
`cli draft-reply <id>` sees the same actionable error message they'd see
from a browser hitting the API.

### `state.py`

**Owns:** the scanner's dedup state. Single SQLite file
(`SCANNER_STATE_DB`, default `scanner_state.db`) with one table:
`processed_messages(message_id, outcome, processed_at)`.

**Outcomes:** `drafted`, `non_travel`, `thread_has_draft`. Storing the
outcome (not just the ID) means we can later analyze classification error
rates without re-processing mail.

**Resource discipline:** every connection is opened with
`contextlib.closing(sqlite3.connect(path))`. `sqlite3.Connection.__exit__`
commits but does **not** close — using `with sqlite3.connect(path) as conn:`
on its own leaks file descriptors. This was a real bug; see §5.4.

The schema check (`CREATE TABLE IF NOT EXISTS` + a `PRAGMA` for the legacy
column) runs on the first connection per DB path and is then cached in a
process-local set. Warm reads skip the migration entirely.

### `kb/`

Required RAG layer. Architecture in detail in
[`kb/README.md`](src/wayonagio_email_agent/kb/README.md). Summary of the
modules and how they wire to the rest of the system:

| Module | Side | Wired to |
|---|---|---|
| `config.py` | both | All other `kb/*` modules call `config.load()`. |
| `drive.py` | ingest only | `kb/ingest.py`. |
| `extract.py` | ingest only | `kb/ingest.py`. |
| `chunk.py` | ingest only | `kb/ingest.py`. |
| `embed.py` | both | `kb/ingest.py` (batch), `kb/retrieve.py` (single query). |
| `store.py` | both | `kb/ingest.py` writes; `kb/retrieve.py` reads. |
| `artifact.py` | both | Same — write side uploads, read side downloads. |
| `ingest.py` | ingest only | `cli kb-ingest` only. |
| `retrieve.py` | runtime only | `llm.client.generate_reply` and `cli kb-search`. |
| `doctor.py` | runtime only | `cli kb-doctor`. Builds a health report (artifact presence, meta, per-source chunk breakdown, embedding-model match, exemplar count) using the same `artifact`/`store` read paths as `retrieve`. |

The KB has its own contract with the rest of the agent:

- `retrieve.retrieve(query)` → `list[ScoredChunk]` or raises `KBUnavailableError`.
- `retrieve.format_reference_block(hits)` → str (or "").
- `KBConfigError` for misconfig (e.g. `KB_RAG_FOLDER_IDS` unset).

Both errors are caught at the API and CLI boundaries and translated to
clean error messages. `llm/client.generate_reply` lets them propagate — no
fallback to ungrounded replies.

### `exemplars/`

Optional, graceful, curator-led companion to the KB. Sets the agency's
**voice** (style, structure, tone) the way the KB sets its **facts**.
Architecture in detail in
[`exemplars/README.md`](src/wayonagio_email_agent/exemplars/README.md).
Summary of the modules and how they wire to the rest of the system:

| Module | Side | Wired to |
|---|---|---|
| `config.py` | runtime | `loader.py`, `cli.exemplar-list`. Reuses `kb.config.parse_folder_id`. |
| `source.py` | runtime | `loader.py`. Calls `kb.drive.build_drive_service` (public alias added for parallel reuse), `kb.drive.list_folder`, `kb.drive.read_file`, `kb.extract.extract_text`. |
| `sanitize.py` | runtime | `source.py` (per-Doc) and the tripwire test. |
| `loader.py` | runtime | `llm.client.generate_reply`, `cli.exemplar-list`, `api._lifespan`. |
| `prompt.py` | runtime | `llm.client.generate_reply`. |

The exemplar contract with the rest of the agent is intentionally narrower
than the KB's, because exemplars must never be load-bearing:

- `loader.get_all_exemplars()` → `list[Exemplar]`. **Never raises.** Empty
  list on every failure path: feature disabled, Drive unreachable, folder
  empty, Doc unreadable, Doc empty after extraction. Failure modes are
  logged at WARNING and cached for the lifetime of the process so a Drive
  outage doesn't trigger per-request retries.
- `prompt.format_exemplar_block(exemplars)` → `str` (or `""`).

Two design choices distinguish exemplars from the KB:

- **No RAG.** The whole pool is raw-injected as one prompt block. At the
  curator-led pool size we expect (10–50 docs), the entire set fits in
  Gemini's context window, the model gets *all* the examples (not just the
  top-K embedding picks), and there is no ingest job, no embedding
  pipeline, no GCS artifact, no schema. The migration door is left open at
  the `loader` boundary — if the pool ever outgrows context, swap in
  embed+top-K behind the same `get_all_exemplars` interface and nothing
  else changes.
- **Cold-start cache + lifespan warm-up.** First call to the loader reads
  Drive once (in parallel via `ThreadPoolExecutor`, ~1s for 30 Docs) and
  caches in memory under double-checked locking. The FastAPI app's
  `lifespan` handler (`api._lifespan`) calls `get_all_exemplars` during
  container startup so the first user request after a Cloud Run cold
  start pays 0ms for exemplars — the cost is moved into the container
  boot window that Cloud Run hides behind its startup probe. The loader's
  never-raises contract means this warm-up cannot block startup; a Drive
  outage at boot just caches `[]` and degrades silently.

**PII sanitization is layered.** The curator's eye is the primary defense
(operators write Docs with placeholders like `<guest>`, `<date>`).
`sanitize.py` is a regex tripwire that runs *after* extraction and *before*
the cache: ordered passes for booking URLs → emails → IBANs → Luhn-valid
card numbers → phone numbers. Order matters because the phone regex
otherwise eats Luhn-valid card numbers; this is documented inline. An
integration test (`tests/test_exemplars_tripwire.py`) feeds deliberately
PII-laced fixtures through the loader end-to-end and asserts that no
sensitive substring survives in the cached `Exemplar.text`.

### `addon/`

Apps Script project for the Gmail Add-on. Reads `BACKEND_URL` and
`BEARER_TOKEN` from script properties, calls `POST /draft-reply` from
`UrlFetchApp`, and shows a notification card with the result. No business
logic; deliberately thin.

---

## 4. The non-negotiable invariants

These are tested in CI and treated as load-bearing:

1. **Draft-only.** `gmail_client` never calls `drafts.send` or
   `messages.send`. Enforced by `TestDraftOnlyInvariant` in `test_agent.py`
   and structurally guaranteed by the OAuth scope list.

2. **KB-required.** `generate_reply` always calls `kb.retrieve.retrieve`
   and propagates `KBUnavailableError`. There is no "draft without KB"
   code path. Enforced by `tests/test_llm.py::TestGenerateReply` and
   surfaced as a 503 in the API.

3. **Bearer-token auth on every mutating endpoint.** `POST /draft-reply`
   requires it; `_verify_token` uses `hmac.compare_digest` (constant time);
   `/healthz` is the only unauthenticated endpoint and it cannot mutate
   anything.

4. **HTTPS-only in production.** Cloud Run terminates TLS automatically;
   self-hosted runs behind Caddy/Nginx. HSTS is set on every response, so
   any client that ever talked to us over HTTPS will refuse plain HTTP
   thereafter.

5. **No PII at rest.** `state.py` stores `(message_id, outcome,
   processed_at)` only. No subjects, no bodies, no addresses. Logs at INFO
   include only message ID + language; DEBUG logs include classifier output
   and must not be enabled in production (called out in the README).

6. **No `ResourceWarning` in tests.** `pyproject.toml`'s
   `filterwarnings = ["error::ResourceWarning", ...]` makes any leaked file
   handle / SQLite connection / socket fail the suite immediately. This
   exists because two real connection leaks slipped through previously
   (see §5.4).

7. **Exemplars never block a draft.** `exemplars.loader.get_all_exemplars`
   is contracted to never raise. Every failure caches `[]` for the process
   lifetime; the `EXAMPLE RESPONSES` block is simply omitted and drafting
   continues with KB-only grounding. The invariant is enforced **twice**
   on purpose: the loader's own broad `try/except` is the primary
   guarantee, and `llm/client.generate_reply` additionally wraps the
   loader call so that even a future regression in the loader's safety
   net (a narrowed `except` clause, an exception type slipping through)
   cannot take down the draft path. Enforced by
   `tests/test_exemplars_loader.py` and the
   `TestGenerateReplyExemplarIntegration` cases in `tests/test_llm.py`.

8. **No PII survives the exemplar pipeline.**
   `tests/test_exemplars_tripwire.py` feeds deliberately PII-laced
   fixtures through the loader end-to-end and asserts that no email,
   phone number, IBAN, Luhn-valid card number, or booking URL substring
   appears in the cached `Exemplar.text`. The curator's manual
   anonymization is the primary defense; the regex pass in
   `exemplars/sanitize.py` is the tripwire.

---

## 5. Cross-cutting concerns

### 5.1 Configuration

All config is environment variables, resolved at the moment of use (not at
import). Library modules (`gmail_client`, `llm.client`, `kb/*`,
`state.py`) **deliberately do not call `load_dotenv`** — that's the
responsibility of the entry points (`api.py`, `cli.py`). This keeps
modules cleanly importable in tests and from other apps without implicit
filesystem reads. Comments in each library module call this out.

`kb/config.py` returns a frozen `KBConfig` dataclass; everything else reads
env directly. The `KBConfig` object exists because the KB has a dozen
related settings that benefit from a single resolved snapshot; everywhere
else, two or three env vars don't justify the indirection.

### 5.2 Error-handling philosophy

Two patterns coexist deliberately:

- **Per-message resilience in the scanner.** `scan_once` catches every
  per-message exception so one bad email never aborts the batch.
  `scan_loop` similarly catches per-iteration exceptions so an outage
  doesn't kill the long-running process. State writes happen *after* the
  side effect so a mid-flow crash retries cleanly.

- **Fail-loud in the manual flow.** `manual_draft_flow` lets exceptions
  propagate; `api.draft_reply` translates them to specific HTTP statuses
  (502/503/500). The Add-on user gets an actionable message, not a generic
  "draft failed" — they can either retry (502) or escalate (503).

The KB-required pivot was the moment we chose fail-loud over graceful
degradation across the board: silent fallback to ungrounded text is worse
than refusing to draft, because staff cannot tell which drafts to trust.

### 5.3 Concurrency model

- **API:** FastAPI threadpool. `draft_reply` is sync `def` (see §3,
  `api.py`); concurrent requests parallelize on threads, not on the event
  loop. Throughput is bounded by `--workers` (uvicorn) and Cloud Run's
  concurrency setting; for this agency neither is the bottleneck.

- **Scanner:** strictly serial. `scan_once` processes one message at a
  time. There is no message-level concurrency — Gmail API quotas and the
  LLM provider's rate limits make per-message work the natural unit, and
  the volume doesn't justify the complexity of parallelism. Two scanner
  instances stepping on each other would also race on the SQLite state
  store.

- **KB cache:** `kb/retrieve.py` uses a process-wide `_state` cache loaded
  on first use under a `threading.Lock` (double-checked locking). The
  index is read-only after load — multiple threads compute cosine
  similarities against the same numpy matrix without coordination.
  `reset_cache()` exists for tests and for a future `POST /kb/reload`
  admin endpoint.

### 5.4 Resource lifecycle

The big one: **`sqlite3.Connection.__exit__` commits the transaction but
does NOT close the connection.** Using `with sqlite3.connect(path) as
conn:` on its own leaks a file descriptor every call. We had this bug in
both `state.py` and `kb/store.py` for weeks before catching it. Both are
now `with closing(sqlite3.connect(path)) as conn, conn:` (close + commit
combined), and `pyproject.toml` upgrades `ResourceWarning` to an error in
the test suite so any future leak (sqlite, file, socket) breaks CI
immediately.

Outside SQLite, `pypdf.PdfReader(io.BytesIO(...))` and the Google Drive
client's `MediaIoBaseDownload` both clean up correctly without explicit
context managers.

### 5.5 Logging and PII

- **INFO** (default in production): message IDs and language codes only.
  Safe to retain.
- **DEBUG**: classifier raw output, scanner state transitions. Includes
  enough to reconstruct what arrived in the inbox; should not be enabled
  in production. The README and AGENTS docs both call this out.
- **WARNING/ERROR**: include exception type and stringified message.
  `exc_info=True` is used at the API boundary so server logs always have
  the full traceback even when the client gets a generic 500.

`state.py` is the only on-disk persistence the agent owns, and it stores
no message content. Drive content lives in Drive (the agency's domain),
the KB index lives in GCS (encrypted at rest by Google), and email content
lives in Gmail (Google).

### 5.6 Testing strategy

- **Mock at the network boundary.** Gmail API calls, LiteLLM calls, and
  the `google.cloud.storage` client are stubbed in tests; everything below
  them runs for real. We don't unit-test trivial passthroughs but we
  fully exercise prompt construction, MIME assembly, KB chunking,
  embedding batching, and the threading/dedup logic in the scanner.

- **Regression tests for past bugs.** `state.py`'s
  `test_get_outcome_does_not_leak_sqlite_connection` and `kb/store.py`'s
  `test_write_and_load_do_not_leak_sqlite_connections` exist because we
  shipped those leaks. The `error::ResourceWarning` filter in
  `pyproject.toml` would also catch them, but explicit tests document the
  failure mode.

- **Coverage targets.** `agent.py` and `api.py` are at 100% — they're the
  orchestration surface and the public contract, both small and worth
  pinning. The remaining gaps are in modules that need real Google
  credentials to exercise (`gmail_client.run_auth_flow`,
  `kb/artifact.upload_artifact` GCS path).

- **`pytest -W error` is the gate.** The full suite passes under strict
  warnings, and CI should be configured to fail on any new warning class.

---

## 6. Where to look first when X breaks

| Symptom | First file to check | Why |
|---|---|---|
| API returns 401 to the Add-on | `api._verify_token`, `AUTH_BEARER_TOKEN` env | Constant-time bearer compare. |
| API returns 503 with "Knowledge base unavailable" | `cli kb-doctor` (diagnoses in one shot), then `kb/retrieve.py`, then `kb/artifact.py` | Index missing in GCS or `KB_LOCAL_DIR`, embedding-model mismatch, or corrupt index. |
| API returns 502 with "LLM returned an empty reply" | `llm/client._chat`, then provider creds | Often Gemini quota or Ollama not running. |
| API returns 503 with "Gmail authentication failed" | `gmail_client.load_credentials`, `token.json` | Refresh token revoked; re-run `cli auth`. |
| Scanner re-drafts the same thread | `state.py`, `gmail_client.thread_has_draft` | Both dedup layers should have caught it. |
| Scanner goes silent, no errors logged | `agent.scan_loop`, then check `LOG_LEVEL` | The loop catches everything; bump to DEBUG. |
| Draft has wrong / no `In-Reply-To` | `agent._build_references`, `gmail_client.draft_reply` | Header chain assembly. |
| Replies in the wrong language | `llm/client.detect_language`, `is_travel_related` | Strict parsing — falls back to `"en"`. |
| Replies hallucinate prices/dates | `kb/retrieve.retrieve`, `kb/embed.py`, the prompt in `llm/client.generate_reply` | KB hit quality — try `cli kb-search` to inspect. |
| Cold start takes 3+ seconds in the API | `kb/retrieve._ensure_loaded`, GCS download | First request triggers index load; expected. |
| Tests fail with `ResourceWarning: unclosed database` | The new test that triggered it | Use `contextlib.closing(sqlite3.connect(...))`. |
| Drafts have the right facts but the wrong tone/structure | `exemplars/loader.py`, then `cli exemplar-list` | Verify the curator's Docs are visible to the runtime; check WARNING logs for failed Drive reads. |
| `cli exemplar-list` shows nothing | `KB_EXEMPLAR_FOLDER_IDS`, then Drive folder permissions | Feature is opt-in; service account needs `drive.readonly` on the folder. |
| WARNING `Could not load exemplars` in startup logs | `exemplars/source.py`, then Drive API quotas | Loader caches `[]` for the lifetime of the process; degradation is silent at runtime. |
| PII appears in a generated draft | `exemplars/sanitize.py`, then the source Doc | Regex tripwire missed something — fix the Doc in Drive, then redeploy or restart to clear the cache. |

---

## 7. Why this shape

The product constraints — single small travel agency, draft-only, low
volume, one operator — drove most of the structural decisions. A few of
them are worth being explicit about because they look "too simple" until
you remember the constraint:

- **No database server.** SQLite for scanner state, SQLite for the KB
  index. Both are small (KBs to MBs, not GBs) and accessed by exactly one
  process. A Postgres dependency would be pure operational tax.

- **No message queue.** The Add-on calls the API synchronously and the
  user waits a few seconds for a draft. With single-digit drafts per day,
  any queueing layer would add more complexity than it removes.

- **No retries.** Per-message failures in the scanner are logged and the
  message is retried on the next pass (because we don't mark it processed
  on failure). Manual drafts surface failures to the user, who clicks
  again. Both are simpler and more correct than baking in an exponential
  backoff.

- **No fancy vector DB.** The KB at full size is a few thousand chunks; a
  numpy matmul against an L2-normalized matrix loaded into RAM beats any
  hosted vector DB on latency and cost. When the corpus grows past
  ~50k chunks the upgrade path is documented in
  [`kb/README.md`](src/wayonagio_email_agent/kb/README.md).

- **Provider-agnostic LLM via LiteLLM.** The agency might stay on Gemini
  Flash for years or might switch to a self-hosted model overnight.
  Making the provider a config knob future-proofs that without code
  churn.

- **KB is required, not optional.** An ungrounded reply that hallucinates
  a price is worse than no reply — staff cannot easily tell which drafts
  to trust. Forcing every draft through the KB collapses that trust
  problem to "is the KB present and fresh?", which is a simple operational
  question.

The codebase's worth lies in **what it doesn't do**: no UI, no auth
service, no DB, no queue, no orchestrator, no plugin system. Each of
those is a deliberate "no" backed by the agency's actual workload, not a
technology preference.

---

## 8. Definition of done for a change

When adding or modifying code, a change is "done" when:

1. **The code compiles cleanly.** `uv run python -c "from
   wayonagio_email_agent import api, agent, cli, ..."` imports without
   error.
2. **The full suite passes under strict warnings.** `uv run pytest -W
   error` shows no failures, no warnings.
3. **No linter errors** in the touched files.
4. **Coverage on `agent.py` and `api.py` stays at 100%.** Other modules
   have softer targets (~85–95%).
5. **No new `ResourceWarning` is suppressed.** If you find one, fix the
   leak; don't add it to the filter list.
6. **Docs are updated where the contract changed.** `README.md` for
   user/operator-facing changes; `AGENTS.md` for agent guidance;
   `kb/README.md` for KB internals; this file for cross-cutting
   architecture.
7. **The draft-only invariant still holds.** `TestDraftOnlyInvariant`
   passes (it always should — that's the point).

If you're adding a new module, write a one-paragraph docstring at the top
that names what the module owns and what it does *not* own. That
discipline is the only reason this codebase reads quickly.
