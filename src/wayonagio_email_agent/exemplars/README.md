# Exemplars (`exemplars/`)

Curator-led example replies that set the agency's **voice**. Optional
companion to the (required) [knowledge base](../kb/README.md): where the
KB grounds replies in *facts*, exemplars shape *style, tone, structure,
and the small phrasings that make a reply read as "from us"*. Opt-in via
`KB_EXEMPLAR_FOLDER_IDS`; **graceful** (every failure path returns `[]`
and is logged at WARNING — exemplars never block a draft); and
deliberately simpler than the KB.

This document explains what the module owns, why it diverges from the KB
on retrieval and failure-handling, what the curator contract is, and the
upgrade path if the curated pool ever outgrows context.

---

## What problem this solves

Even with a perfect KB, an ungrounded LLM defaults to a generic tone:
formal, slightly hedged, full of "I would be happy to assist". The
agency's actual voice is warmer, shorter, and tends to lead with the
travel detail rather than the boilerplate. We want the LLM to mirror that
voice without the operator having to write a prompt-engineered style guide.

Few-shot example replies do this trick at almost no engineering cost. The
agency owner curates a small folder in Drive — one Google Doc per example
— and at draft time the agent injects every Doc as an `EXAMPLE RESPONSES`
block in the prompt. The framing tells the model: *"mirror the style and
structure; defer to the REFERENCE MATERIAL above for facts."* That's the
whole feature.

The same Drive surface that runs the KB is reused for storage; no new
storage system, no new auth scope, no new ingest pipeline.

---

## Architecture at a glance

```
┌──────────────────────────── COLD START (api lifespan) ────────────────────────────┐
│                                                                                   │
│  api._lifespan  ──▶  loader.get_all_exemplars()  ──▶  source.collect()            │
│                                                       │                           │
│                                                       ▼                           │
│                                          kb.drive.list_folder (sequential)        │
│                                                       │                           │
│                                                       ▼                           │
│                              ThreadPoolExecutor(max_workers=8) per Doc:           │
│                                  kb.drive.read_file  ─▶  kb.extract.extract_text  │
│                                                       │                           │
│                                                       ▼                           │
│                                            sanitize.sanitize (regex tripwire)     │
│                                                       │                           │
│                                                       ▼                           │
│                                       cache: list[Exemplar] (process-wide)        │
└───────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────── PER-REQUEST (warm path) ──────────────────────────────┐
│                                                                                   │
│  llm.generate_reply  ──▶  loader.get_all_exemplars()   ── O(1), cache hit         │
│                       ──▶  prompt.format_exemplar_block(...)  ── single string    │
│                       ──▶  user_content +=  reference_block + exemplar_block      │
│                                                                                   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

The two flows share the cache. After warm-up, every draft pays exactly
one O(1) dict-lookup and one string concatenation for exemplars.

---

## Why this isn't shaped like the KB

The KB and the exemplars module solve adjacent problems but their shapes
diverge on purpose. Three big differences:

### 1. No retrieval, no embeddings, no ingest job

The KB chunks documents, embeds them, stores them in SQLite, and at
runtime embeds the user's message and runs cosine similarity to pick the
top-K chunks. That whole pipeline exists because the KB content (tour
descriptions, FAQs, policies) is large enough that we *can't* fit it all
in the LLM context window — top-K is a necessity, not a choice.

The curated exemplar pool is small by definition: 10–50 Docs, each
typically 100–500 tokens. The whole pool fits comfortably in Gemini's
context window with room to spare. Given that, retrieval would *cost*:

- An extra embedding round-trip per draft (~100ms).
- An ingest pipeline (chunk + embed + store + publish to GCS).
- An artifact lifecycle (versioning, refresh, cache invalidation).
- An operational debugging surface ("did the right exemplar match?").

…in exchange for selecting maybe the 3 most relevant examples instead of
showing all 30. Few-shot prompting is *better* with more examples (within
context), so that tradeoff is actively negative on quality too.

The decision: **raw injection, the entire pool every time, cached in
memory.** The migration door is open if the pool grows past context (see
[§Upgrade path](#upgrade-path)).

### 2. Optional and graceful, not required and fail-loud

The KB is the only thing standing between the LLM and a confident
hallucination about prices and policies; an ungrounded reply is worse than
no reply because staff cannot cheaply tell the difference. So KB failures
abort drafting (503 from the API) by design.

Exemplar failures are different in kind: the worst case is a draft in the
LLM's default tone instead of the agency's curated voice. Staff still get
a draft, the KB still grounds the facts, and the operator can investigate
later. Hard-failing the draft because a Drive call timed out would trade
a small style regression for a complete outage — a strictly bad trade.

So `loader.get_all_exemplars()` is contracted to **never raise**. Every
failure path catches and caches `[]`:

- `KB_EXEMPLAR_FOLDER_IDS` unset → empty list, no log spam.
- Drive 5xx / timeout / auth failure → empty list, WARNING with
  `exc_info`.
- A specific Doc unreadable / extraction fails → log WARNING for that
  Doc, continue with the rest.
- Doc extracts to empty text → skip with WARNING.

The cached `[]` lives for the lifetime of the process. That's deliberate:
a Drive outage at startup shouldn't trigger per-request retries that
hammer Drive while it's down. A new Cloud Run revision (or process
restart) is the explicit "try again" signal.

### 3. Cold-start cache instead of an artifact

The KB writes a versioned artifact (`kb_index.sqlite` in GCS) that the
runtime downloads on cold start. That makes sense because building the
index is expensive (embedding the whole corpus) and you want every
runtime instance to reuse the same precomputed result.

Exemplars don't need that: the most expensive operation is a parallel
Drive read, which is already fast (~1s for 30 Docs) and gets faster with
the thread pool. Building an artifact pipeline for content this cheap to
read would be ceremony for ceremony's sake.

Instead, each runtime process reads Drive once on startup and caches the
result in a module-level variable. The cache uses double-checked locking
(`threading.Lock` + recheck) so the FastAPI threadpool can't trigger
concurrent collection during a race at startup. After that, every reader
is lock-free.

The FastAPI app installs a `lifespan` handler that triggers the load
*before* the instance accepts traffic, moving the Drive cost into the
container's startup window (which Cloud Run hides behind its startup
probe). This means the first user request after a cold start pays 0ms
for exemplars.

---

## The curator contract

The curator (the agency owner, in practice) is the primary defense for
quality and the primary defense for PII. The pipeline is built around
that fact.

**Doc structure.** One Doc = one exemplar. The Doc title becomes the
exemplar's heading in the prompt, so write titles as descriptions of what
the example covers ("Refund policy for weather cancellations", "Altitude
sickness preparation reply") rather than internal slugs. The Doc body is
one self-contained example reply: a Q+A pair, a template, or an
illustrative thread excerpt. Markdown is fine; there's no schema. We
don't parse headings, don't split on sections, don't extract metadata.

**No real names, ever.** Anonymize while you write: `<guest>` for the
client name, `<tour-name>` for the booking, `<date>`, `<amount>`, etc.
The curator's eye catches things no regex ever will (reversed digits in
dates, partial phone numbers split across lines, names embedded in URLs).

**The regex tripwire** (`sanitize.py`) catches the obvious mechanical
leaks the curator might miss. Ordered passes for booking URLs, emails,
IBANs, Luhn-valid card numbers, and phone numbers. Anything caught is
silently redacted in the cached text and logged at WARNING. The order
matters — the phone regex would otherwise eat Luhn-valid card numbers,
because a 16-digit run looks like a phone number with extra digits.
There's a unit test for the ordering and a CI tripwire test
(`tests/test_exemplars_tripwire.py`) that feeds deliberately PII-laced
fixtures end-to-end through the loader and asserts no PII substring
survives.

**One Drive folder, shared with the same service account.** Use the same
service account that already has `KB_RAG_FOLDER_IDS` access, with the
same `drive.readonly` scope. No new OAuth grant, no new credential.
Folder ID(s) go in `KB_EXEMPLAR_FOLDER_IDS`.

**Editing workflow.** Edit a Doc in Drive. Changes become visible to the
runtime after the next process cold start. For an immediate refresh,
trigger a new Cloud Run revision rollout. There is no "reload exemplars"
endpoint by design — exemplars change rarely (the curator publishes a
batch every few weeks at most), and "redeploy" is a clearer signal than a
side-channel reload that could accidentally fire from the public network.

**Sanity check what the runtime sees:**

```bash
uv run python -m wayonagio_email_agent.cli exemplar-list
```

This prints each cached exemplar's title, ID, and a short body preview
(post-sanitization) in the order they will appear in the prompt. Use it
after a curator edit to confirm the change landed and that PII redaction
didn't accidentally swallow something it shouldn't have.

---

## Module reference

### `config.py`

`ExemplarConfig` dataclass + `load()`. Resolves `KB_EXEMPLAR_FOLDER_IDS`
and (optional) `KB_EXEMPLAR_INCLUDE_MIME_TYPES` from env. Reuses
`kb.config.parse_folder_id` so a Drive share URL works the same way it
does for the KB. `enabled` is a property: `True` iff at least one folder
ID resolved.

Does not raise on missing config; an unset folder list is the documented
"feature off" signal, not an error.

### `source.py`

`collect(cfg, *, service=None, max_workers=8)` returns
`list[Exemplar(title, text, source_id)]`. Walks each configured folder
(sequential — folder lists are usually flat), then fans out individual
Doc reads across `ThreadPoolExecutor(max_workers=8)`. Each worker calls
`kb.drive.read_file` → `kb.extract.extract_text` → `sanitize.sanitize`.

Exposes `kb.drive.build_drive_service()` (the public alias added for this
module) so all worker threads share one authenticated Drive client
instead of authenticating per call. Per-Doc failures are logged at
WARNING and skipped; the rest of the batch proceeds. Empty extractions
are skipped with a WARNING (a Doc that comes back empty after extraction
is almost always a curator mistake worth surfacing).

Results are sorted by title to keep the prompt block stable across
processes — same Drive folder, same prompt, regardless of which worker
finished first.

### `sanitize.py`

PII tripwire. Implements `sanitize(text)` as five ordered regex passes:

1. **`<BOOKING_URL>`** — URLs with a booking-id-shaped query parameter.
   Run first because the URL pattern often contains digits that other
   regexes would otherwise grab.
2. **`<EMAIL>`** — RFC-shaped email addresses.
3. **`<IBAN>`** — IBAN-format strings (country code + check digits +
   alphanumeric block).
4. **`<CARD>`** — runs of 13–19 digits (with optional separators) that
   pass the Luhn checksum. Specifically Luhn-valid to avoid eating
   booking reference numbers that *look* like card numbers.
5. **`<PHONE>`** — runs of 7–15 digits with optional country code and
   separators.

The order is the load-bearing detail: cards must be redacted *before*
phones, otherwise the phone pattern eats Luhn-valid 16-digit card
numbers because they fit the "long run of digits" shape too. There's a
table-driven unit test for each pattern and a separate test
(`test_long_non_luhn_digit_run_is_left_alone`) documenting that we
deliberately don't redact long *non-Luhn* digit runs as cards — those are
booking references, not credit cards.

This is a tripwire, not the primary defense. The curator's manual
anonymization (placeholders like `<guest>`, `<date>`) is what we rely on
for completeness; the regex catches mechanical slips.

### `loader.py`

Process-level cache with double-checked locking. Public surface is two
functions:

- `get_all_exemplars() -> list[Exemplar]` — never raises; returns the
  cached list (or builds it if cold).
- `reset()` — test-only; clears the cache so subsequent `get_*` calls
  re-collect.

The double-checked pattern means the lock is only contended on the very
first call after process start; warm reads are lock-free. On the cold
path: load config, call `source.collect`, cache the result. On any
exception during collection: log WARNING with `exc_info`, cache `[]`,
return `[]`. The cached empty list lives for the rest of the process —
no per-request retries against a Drive that's down.

The "never raises" contract is a hard one. The integration tests in
`tests/test_exemplars_loader.py` and the lifespan tests in
`tests/test_api.py::TestExemplarWarmup` both pin it.

**The contract is defended twice.** `llm/client.generate_reply` *also*
wraps the loader call in a `try/except` that logs a WARNING and falls
back to `[]`. That belt-and-suspenders pattern looks redundant in the
happy path, but it's load-bearing for the architectural promise that
exemplars are *optional and graceful* — a single regression in the
loader's broad `except` clause (someone narrows it during a refactor,
or a `BaseException` subclass slips through) would otherwise break the
whole draft path for a feature that's supposed to be optional.
`tests/test_llm.py::TestGenerateReplyExemplarIntegration::test_exemplar_loader_exception_does_not_block_drafting`
pins this defensive layer.

### `prompt.py`

`format_exemplar_block(exemplars: list[Exemplar]) -> str`. Returns `""`
for an empty list (so the LLM client can append unconditionally). For a
non-empty list, formats the `EXAMPLE RESPONSES` block with explicit
framing:

```
EXAMPLE RESPONSES (style and structure reference only):
The REFERENCE MATERIAL above is authoritative for facts; the examples
below are for tone, structure, and phrasing. If an example contradicts
the reference material on a fact, the reference material wins.

[1] <title 1>
<text 1>

[2] <title 2>
<text 2>
...
```

The framing is the load-bearing piece. Without it, exemplars start
acting as fact sources and contradict the KB-required invariant. The
order in `llm/client.py` (KB block first, exemplar block second) is what
makes the word "above" actually point at the right place. Don't reorder
the two blocks without updating the framing.

`llm/client.py` additionally **suppresses the exemplar block whenever
the KB returned no hits**, even when exemplars are present and healthy.
The framing depends on the reference block existing ("above"), so
without one the prompt would contradict itself. There's also a safety
argument: exemplars without KB grounding can lure the model into
copying example facts (prices, durations) verbatim with no canonical
source to override them. The KB nearly always returns hits in
production, so this guard fires only in pathological edge cases
(`top_k=0`, an empty index) — but it keeps the prompt coherent rather
than self-contradictory in those cases.

---

## Performance notes

- **Cold start cost.** Sequential folder listing (~100ms per folder) plus
  parallel per-Doc reads (~1s for 30 Docs at `max_workers=8`). Hidden
  inside Cloud Run's startup probe window via the `lifespan` warm-up.
- **Warm request cost.** One dict lookup + one string concatenation.
  Effectively free.
- **Memory.** ~30 Docs × ~500 tokens × ~5 bytes/token ≈ 75KB per
  process. Negligible.
- **Drive API quotas.** One read per Doc per process lifetime. At
  `min-instances=1` that's one batch per deploy; at the default
  `min-instances=0` it's one batch per cold start.

If you want zero cold-start latency variance between deploys, set
`--min-instances=1` on the Cloud Run service (~$5–10/month). The
warm-up runs once at deploy time and every subsequent request reuses the
cached pool. Without it, Cloud Run still calls the warm-up on each cold
start, so the first user request is unaffected — only the container's
cold-boot latency itself is slightly longer (and is hidden by the
startup probe).

---

## Failure modes and how they look

| Failure | Logged | User-visible effect |
|---|---|---|
| `KB_EXEMPLAR_FOLDER_IDS` unset | (silent — feature off) | No `EXAMPLE RESPONSES` block; KB still grounds the draft. |
| Drive 5xx during cold-start collection | WARNING with `exc_info` | Cache populated with `[]`; drafts continue without exemplars for the lifetime of the process. |
| One Doc fails to read or extract | WARNING with the Doc ID | Other exemplars still cached and used. |
| Doc extracts to empty string | WARNING with the Doc ID | Exemplar dropped; rest used. |
| Sanitizer redacts something | WARNING (sanitization is silent in the cached text but the WARNING in source.py flags it) | Curator should fix the Doc. |
| Process restart after a curator edit | (silent — expected) | New cache reads the updated Doc. |

None of these surfaces as a 5xx from the API. If you start seeing
WARNING `Could not load exemplars` consistently, check Drive folder
permissions and the service account's `drive.readonly` scope.

---

## Upgrade path

The curated pool size assumption (10–50 Docs) is what makes raw injection
work. If the agency ever wants to publish hundreds of exemplars (e.g.
one per booking type per season) and the prompt starts blowing past
context limits or tone-mirroring quality starts dropping because the LLM
can't attend to all of them, there is an explicit migration path that
doesn't touch any code outside this module:

1. Add an embed step inside `loader.collect` (or a new `embed.py`
   parallel to `kb/embed.py`).
2. Add a top-K retrieval call inside `get_all_exemplars` (or expose a
   new `get_relevant_exemplars(query)` function and update
   `llm/client.py` to pass the query in).
3. Optionally cache the embeddings to GCS on the same artifact pattern
   the KB uses, so cold starts don't re-embed.

The boundary at `loader.get_all_exemplars()` was chosen specifically so
that swap is local. The rest of the agent — the LLM client, the API,
the CLI, the prompt block — wouldn't need to change.

The reverse is also true: if you ever decide exemplars are noise, deleting
the module and the `KB_EXEMPLAR_FOLDER_IDS` reference is a clean removal.
The agent has no hard dependency on it.

---

## Testing

Coverage of the module is structured around the three contracts:

- **Sanitization completeness.**
  `tests/test_exemplars_sanitize.py` is table-driven across each PII
  class with both positive (must redact) and negative (must leave alone)
  cases. The tricky cards-vs-phones ordering is explicitly pinned.
- **Loader's never-raises contract.**
  `tests/test_exemplars_loader.py` injects raising stubs at every layer
  and asserts the loader still returns `[]` and the warning is logged.
- **End-to-end PII tripwire.**
  `tests/test_exemplars_tripwire.py` simulates a curator publishing Docs
  with deliberately-leaked PII and asserts that nothing sensitive
  survives to the cached `Exemplar.text`. This is the regression net for
  the whole pipeline; if anyone changes the regex ordering or the
  sanitization step, this test must still pass.

There are also integration tests against the LLM client
(`tests/test_llm.py::TestGenerateReplyExemplarIntegration`) that pin the
prompt-block ordering (KB block before exemplar block) and that
exemplar load failures don't break drafting.
