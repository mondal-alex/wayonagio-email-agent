# Wayonagio Email Agent

A **draft-only** email response agent for a Cusco (Peru) travel agency. It connects to Gmail via the Gmail API and uses an LLM to generate multilingual draft replies (Italian, Spanish, English). Staff trigger drafts via a Gmail Add-on button; an automatic scanner creates drafts for new travel-related emails.

It uses a **layered optimization stack** so drafts are grounded, on-brand, and resilient:

- **Knowledge base RAG (required):** Google Drive content is chunked + embedded; replies are grounded with retrieved `REFERENCE MATERIAL`.
- **Exemplars (optional):** curator-written sample replies shape tone/style via an `EXAMPLE RESPONSES` prompt block.
- **Full thread context:** drafts use the complete thread (first contact through the selected message), not just one email.
- **Latest-turn prioritization:** the model answers the most recent customer turn first and treats earlier turns as context only.
- **Operational resilience:** retries/backoff for transient Gemini/Gmail failures, plus explicit logging for thread load/truncation.

**The agent never sends email.** It only calls `drafts.create`. Sending is always done manually by staff in Gmail.

LLM calls go through [LiteLLM](https://docs.litellm.ai/), which lets you swap providers without code changes. Two supported setups:

- **Google Gemini API** (recommended for production) — pay-per-use, no hardware to maintain. See [Recommended deployment: Cloud Run + Gemini](#recommended-deployment-cloud-run--gemini).
- **Self-hosted Ollama** (recommended for local dev or fully offline deployments) — no external API calls, runs on your own box.

## Prerequisites

- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with the **Gmail API** and **Google Drive API** enabled, and OAuth 2.0 desktop credentials downloaded as `credentials.json`. Full walkthrough: [Google Cloud / Gmail setup](#google-cloud--gmail-setup) (short version) or `[docs/LOCAL_TESTING.md` → Create Gmail + Drive OAuth credentials](docs/LOCAL_TESTING.md#1-create-gmail--drive-oauth-credentials) (step-by-step with screenshots-worth of detail).
- A Drive folder the agent can read for the knowledge base — every draft is grounded in agency content. Any folder of tour descriptions, FAQs, or templates works; see [Knowledge base (required)](#knowledge-base-required).
- **One** of:
  - A [Google AI Studio API key](https://aistudio.google.com/app/apikey) for Gemini, **or**
  - [Ollama](https://ollama.com) running locally or on the server (`ollama serve`)

> Even when you use Ollama as the LLM, the knowledge base defaults to Gemini embeddings (`gemini/gemini-embedding-001`), so an AI Studio key is typically still needed. See `[docs/LOCAL_TESTING.md` → Configure the knowledge base](docs/LOCAL_TESTING.md#configure-the-knowledge-base) for the fully-offline alternative.

## Google Cloud / Gmail setup

1. Go to [Google Cloud Console](https://console.cloud.google.com) → **APIs & Services** → **Library**.
2. Enable the **Gmail API**.
3. In the same Library, enable the **Google Drive API** (the knowledge base reads agency content from Drive).
4. Go to **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth client ID**.
  - Application type: **Desktop app**
5. Download the JSON file and save it as `credentials.json` in the project root.
6. Go to **OAuth consent screen / Data access** and add these scopes:
  - `https://www.googleapis.com/auth/gmail.readonly`
  - `https://www.googleapis.com/auth/gmail.compose`
  - `https://www.googleapis.com/auth/drive.readonly`
7. Pick the **User type** on the OAuth consent screen:
  - **Internal** if the project is owned by a Google Workspace organization (e.g. `@wayonagio.com` via Workspace). Verify at **IAM & Admin → Settings → Organization**. No test-user list needed, refresh tokens don't expire.
  - **External** otherwise. While in **Testing**, add the Gmail account as a test user, and note that refresh tokens rotate every 7 days — either move to a Workspace tenant or publish the OAuth app before running the scanner unattended in production.
   See `[docs/LOCAL_TESTING.md` → step 7](docs/LOCAL_TESTING.md#1-create-gmail--drive-oauth-credentials) for the fuller rationale.

## Installation

```bash
uv sync
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```


| Variable                 | Scope                | Description                                                                                                                                                    |
| ------------------------ | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GMAIL_CREDENTIALS_PATH` | always               | Path to `credentials.json` (OAuth client secrets)                                                                                                              |
| `GMAIL_TOKEN_PATH`       | always               | Path for persisted OAuth token (written by `cli auth`)                                                                                                         |
| `LLM_MODEL`              | always               | LiteLLM model string: `gemini/gemini-2.5-flash`, `ollama/llama3.2`, etc.                                                                                       |
| `GEMINI_API_KEY`         | Gemini only          | Google AI Studio API key. Required when `LLM_MODEL` starts with `gemini/`.                                                                                     |
| `OLLAMA_BASE_URL`        | Ollama only          | Ollama server URL (default: `http://localhost:11434`)                                                                                                          |
| `OLLAMA_MODEL`           | Ollama only (legacy) | Back-compat: if `LLM_MODEL` is unset, we use `ollama/<OLLAMA_MODEL>`.                                                                                          |
| `OLLAMA_KEEP_ALIVE`      | Ollama only          | How long Ollama keeps the model loaded (e.g. `5m`, `1h`, `-1`). Default `1h`.                                                                                  |
| `AUTH_BEARER_TOKEN`      | always               | Bearer token for API authentication                                                                                                                            |
| `SCANNER_ENABLED`        | always               | Feature flag for automatic scanner (`false` by default)                                                                                                        |
| `SCANNER_STATE_DB`       | always               | SQLite DB path for scanner dedup state (default: `scanner_state.db`)                                                                                           |
| `LOG_LEVEL`              | always               | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`)                                                                                           |
| `KB_EXEMPLAR_FOLDER_IDS` | optional             | Comma-separated Drive folder IDs/URLs containing curator-written example replies. Empty = exemplars disabled. See [Exemplars (optional)](#exemplars-optional). |


### Choosing an LLM provider

Set `LLM_MODEL` to a LiteLLM `<provider>/<model>` string.


| Use case                              | Set `LLM_MODEL` to                                             | Also set                               |
| ------------------------------------- | -------------------------------------------------------------- | -------------------------------------- |
| Production on Cloud Run (recommended) | `gemini/gemini-2.5-flash`                                      | `GEMINI_API_KEY`                       |
| Local development, fully offline      | `ollama/llama3.2`                                              | `OLLAMA_BASE_URL`, `OLLAMA_KEEP_ALIVE` |
| Other LiteLLM-supported provider      | see the [LiteLLM docs](https://docs.litellm.ai/docs/providers) | provider-specific env vars             |


Because LiteLLM is provider-agnostic, adding another backend (OpenAI, Anthropic, Azure, Vertex, etc.) is a config change, not a code change.

### Generate `AUTH_BEARER_TOKEN`

Create a strong random token and set it in `.env`:

```bash
openssl rand -base64 32
```

Or with Python:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then paste it into `.env`:

```env
AUTH_BEARER_TOKEN=<paste-token-here>
```

Use the same value for the Gmail Add-on Script Property `BEARER_TOKEN`.

### Regenerate (rotate) `AUTH_BEARER_TOKEN`

Rotate the bearer token whenever it has been exposed (e.g. pasted in chat/screenshots, committed accidentally, shared with someone who no longer needs access) and on a regular schedule for defense in depth.

Rotation is a coordinated change: the backend `.env` and the Gmail Add-on Script Property must both hold the **same** new value, or the Add-on will get `401 Unauthorized` on every call.

1. **Generate a new token** (same command as above):
  ```bash
   openssl rand -base64 32
  ```
2. **Update `.env`** on the server. Replace the existing `AUTH_BEARER_TOKEN=...` line with the new value.
3. **Restart the API server** so it picks up the new env var:
  ```bash
   uv run uvicorn wayonagio_email_agent.api:app --host 0.0.0.0
  ```
   (Or `systemctl restart wayonagio-email-agent` if you run it as a systemd service.)
4. **Update the Gmail Add-on Script Property.** In Apps Script → **⚙️ Project Settings → Script Properties**, edit `BEARER_TOKEN` to the new value and save. No redeploy or re-authorization needed.
5. **Verify** with a quick end-to-end test: click a draft button in Gmail. A `401` notification means the values don't match — re-check both.
6. **Invalidate the old token** if it was shared: because the server only knows the current value, once `.env` is updated and the API is restarted, the old token no longer works. Nothing else to do.

Tips:

- If you run multiple backend instances (e.g. behind a load balancer), roll the new `.env` to every instance before updating the Add-on Script Property, so the Add-on doesn't hit a stale instance with the old token.
- Never commit `.env`, and never paste `AUTH_BEARER_TOKEN` into issue trackers, terminals you share-screen from, or chat logs. If you do, rotate immediately.
- Rotate the token on a regular cadence (e.g. every 90 days) even if nothing has been leaked. Remember that **Apps Script Script Properties are not true secrets** — anyone with edit access to the Apps Script project can read `BEARER_TOKEN`. Scale-down the number of editors accordingly.

## Security and privacy posture

This service is simple by design. The security posture below is what keeps it trustworthy.

**Draft-only invariant (defense in depth)**

- OAuth scopes are `gmail.readonly` + `gmail.compose` only. `**gmail.send` is intentionally absent**, so the server physically cannot send mail even if compromised.
- A regression test (`TestDraftOnlyInvariant`) asserts that `drafts.send` and `messages.send` are never invoked in either the manual or scanner flow — baked into CI.

**Authentication and transport**

- Every `POST /draft-reply` request must carry `Authorization: Bearer <AUTH_BEARER_TOKEN>`.
- The token is compared with `hmac.compare_digest`, so byte-level timing leaks aren't possible.
- HTTPS/TLS is non-negotiable. Cloud Run terminates TLS automatically. Self-hosted must run behind Caddy or Nginx — never expose the uvicorn port directly.
- HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer` are added to every response.

**Abuse / DoS hardening**

- Request bodies are capped at 16 KiB at the middleware layer; larger requests get `413` without touching the handler.
- On Cloud Run, use `--max-instances=2` (see the deploy command) to cap burn rate if the bearer token is ever leaked. Combined with the pay-per-use Gemini model, the blast radius of a leaked token is bounded in both time (until rotation) and cost (until max-instances is saturated).

**Logging and PII**

- **Do not run with `LOG_LEVEL=DEBUG` in production.** DEBUG logs include LLM classifier outputs and message-ID/sender metadata. INFO (the default) logs only the message ID and language for each request.
- Set an explicit retention policy on your Cloud Logging `_Default` bucket. The platform default is 30 days; set it shorter if your privacy policy requires it:
  ```bash
  gcloud logging buckets update _Default --location=global --retention-days=14
  ```
- The agent never persists message content or replies to its own storage. The only on-disk state is `scanner_state.db`, which stores message IDs and outcome labels (`drafted` / `non_travel` / `thread_has_draft`) — no bodies, subjects, or addresses.

**Threat model, explicitly**

- **In scope**: An attacker on the public internet who finds the Cloud Run URL. They can't authenticate without the bearer token and can't send mail even if they steal the token (only draft).
- **In scope**: Accidental token leakage (screenshot, git push). Mitigated by documented rotation and a 90-day cadence.
- **Out of scope**: A compromised Google account of a Wayonagio staff member. That's a Workspace-level problem — enable 2FA and phishing-resistant MFA on all staff accounts.
- **Out of scope**: A compromised Cloud project (someone with `roles/editor` on your GCP project). They can read your Secret Manager secrets. Keep project IAM tight; only project owner + one backup.

## First-time authentication

Run this **once on the server** to open a browser, complete the OAuth flow, and write `token.json`:

```bash
uv run python -m wayonagio_email_agent.cli auth
```

The token is refreshed automatically on subsequent runs. If the refresh token is ever revoked, re-run `cli auth`.

Mailbox model note:

- The backend uses the Gmail account tied to `token.json` (`userId="me"` in Gmail API calls).
- Drafts are created in that same mailbox.
- For agency rollout, use a shared agency inbox/account for backend OAuth and have staff use the Add-on in that same account context.

## Running

Recommended rollout:

- Start with the API server + Gmail Add-on only.
- Leave `SCANNER_ENABLED=false` for the initial manual-draft rollout.
- Enable the scanner later by setting `SCANNER_ENABLED=true` once the team is ready for automatic pre-drafting.

### API server (used by the Gmail Add-on)

```bash
uv run uvicorn wayonagio_email_agent.api:app --host 0.0.0.0
```

Must be run behind a reverse proxy (Caddy or Nginx) that terminates HTTPS. See [Server deployment](#server-deployment).

### Automatic scanner

The scanner is feature-flagged off by default. To enable it:

```bash
export SCANNER_ENABLED=true
```

Or set `SCANNER_ENABLED=true` in `.env`.

There are two scanner entry points, one for always-on hosts and one for schedulers:

```bash
# Long-running loop: use on a VM, a systemd service, or any
# always-on container. NOT suitable for Cloud Run, which scales
# idle instances to zero and will kill the loop.
uv run python -m wayonagio_email_agent.cli scan --interval 1800

# One-shot pass: runs a single scan and exits. Designed for
# external schedulers (Cloud Scheduler -> Cloud Run Jobs, cron,
# systemd timers) that own the cadence and the process lifecycle.
uv run python -m wayonagio_email_agent.cli scan-once

# Test classification without creating any drafts (works for both).
uv run python -m wayonagio_email_agent.cli scan --dry-run
uv run python -m wayonagio_email_agent.cli scan-once --dry-run
```

Scanner behavior:

- It polls unread mail with `is:unread`.
- Each message ID is recorded in the local SQLite state DB after a final scanner outcome: `drafted`, `non_travel`, or `thread_has_draft`.
- This prevents repeated LLM classification of the same unread non-travel messages across scans and restarts.
- If Gmail draft lookup fails for a thread, the scanner skips that message for the current iteration instead of assuming it is safe to draft.
- `--dry-run` does not create drafts and does not persist scanner state.
- If `SCANNER_ENABLED=false`, both `scan` and `scan-once` exit immediately with a clear error instead of starting.

**Recommended Cloud Run pattern**

Cloud Run's API service is for the Gmail Add-on's synchronous `POST /draft-reply` traffic. The scanner doesn't fit that model — so run it as a **Cloud Run Job** triggered by **Cloud Scheduler** on a fixed cron (e.g. every 30 minutes). The Job container entrypoint is simply:

```
python -m wayonagio_email_agent.cli scan-once
```

This way each scan is its own short-lived execution, Cloud Run's scale-to-zero behavior is a feature rather than a hazard, and the scheduler — not the process — owns the interval.

### CLI (admin)

```bash
# List recent unread emails
uv run python -m wayonagio_email_agent.cli list

# Create a draft for a specific message
uv run python -m wayonagio_email_agent.cli draft-reply <message_id>

# Rebuild the knowledge base from Drive (see Knowledge base below)
uv run python -m wayonagio_email_agent.cli kb-ingest

# Debug retrieval for a query
uv run python -m wayonagio_email_agent.cli kb-search "how much is Machu Picchu?"

# One-shot KB health check: is the artifact present, what's in it,
# when was it last ingested, does the embedding model match?
uv run python -m wayonagio_email_agent.cli kb-doctor

# List the exemplars the runtime has cached (post-sanitization).
uv run python -m wayonagio_email_agent.cli exemplar-list
```

### Knowledge base (required)

The knowledge base is a **required** RAG component that reads content from Google Drive and grounds every draft in agency-specific facts. Without it the agent refuses to draft — an ungrounded reply that staff might send unmodified is worse than no reply at all. Point `KB_RAG_FOLDER_IDS` at whatever Drive folder IDs (or share URLs) the agency already uses for tour descriptions, FAQs, and templates — no folder renaming required.

The ingest pipeline walks every configured folder (recursively, by default), extracts text from Google Docs, PDFs, plain text, and Markdown, chunks and embeds it, and publishes a single artifact:

- `kb_index.sqlite` — vector index with embeddings (default model: `gemini/gemini-embedding-001`, dimension 3072).

Artifacts land in `KB_GCS_URI` (Cloud Run) or `KB_LOCAL_DIR` (dev / single-host). The API and scanner load the index at cold start. KB failures (no artifact published yet, GCS unreachable, embedding API down, embedding-model mismatch) raise `KBUnavailableError` and the draft request fails with a clear error rather than degrading silently.

Environment variables (see [.env.example](.env.example) for the full list):


| Variable             | Description                                                                              |
| -------------------- | ---------------------------------------------------------------------------------------- |
| `KB_RAG_FOLDER_IDS`  | **Required.** Comma-separated Drive folder IDs or share URLs. Drafting fails without it. |
| `KB_RAG_RECURSIVE`   | Walk subfolders (default `true`).                                                        |
| `KB_EMBEDDING_MODEL` | LiteLLM model string for embeddings (default `gemini/gemini-embedding-001`).             |
| `KB_GCS_URI`         | Production: `gs://bucket[/prefix]`. The index lives here.                                |
| `KB_LOCAL_DIR`       | Dev fallback when `KB_GCS_URI` is unset (default `./kb_artifacts`).                      |
| `KB_TOP_K`           | Chunks to retrieve per email (default `4`).                                              |


**First-run order matters.** Before the API can serve any draft, run `cli kb-ingest` once so a fresh `kb_index.sqlite` exists at `KB_GCS_URI` / `KB_LOCAL_DIR`.

**Drive OAuth scope.** The agent authentication requests `drive.readonly` alongside the two Gmail scopes; `cli auth` asks for all three up front. If you authenticated before this feature existed, delete `token.json` and re-run `cli auth` once.

**Cadence.** The agency edits this material roughly **once a year** (the seasonal refresh of tour catalogs, prices, and policies). That's a strong feature: the KB has near-zero risk of going stale between updates, and re-ingest costs (embedding API calls + GCS write) are essentially a once-a-year line item rather than a recurring expense. The flip side is operational: **schedule a yearly re-ingest** and treat it as a calendar reminder, not a daily background job. Any content edit out of cycle should also trigger an on-demand `cli kb-ingest` so the next draft sees it.

**Cloud Run deployment.** In production the ingest pipeline runs as a **Cloud Run Job** triggered by **Cloud Scheduler** on a yearly cadence that matches the agency's editorial rhythm. The full command sequence (bucket + Job + scheduler + first manual run + verification with `kb-doctor`) lives in [Recommended deployment: Cloud Run + Gemini / step 6](#6-populate-the-knowledge-base) so the deploy story reads top-to-bottom in one place. The Job entrypoint is `python -m wayonagio_email_agent.cli kb-ingest`; its service account needs `roles/storage.objectAdmin` on the `KB_GCS_URI` bucket and `roles/secretmanager.secretAccessor` on the same secrets as the API service. Reuse the API's service account unless you have a reason to split them.

You can sanity-check retrieval without ever sending an email:

```bash
# High-level: is the KB healthy? What does it think it has indexed?
uv run python -m wayonagio_email_agent.cli kb-doctor

# Low-level: what chunks does retrieval return for a realistic question?
uv run python -m wayonagio_email_agent.cli kb-search "inca trail permit availability"
```

`kb-doctor` is the one command to reach for when drafts start 503-ing. It reports artifact presence, ingest timestamp and age, chunk count, per-source chunk breakdown, the embedding model the index was built with (and whether it matches the runtime), and the size of the exemplar pool. Exits non-zero on any hard failure (missing artifact, empty index, embedding-model mismatch), which makes it safe to wire into a deploy smoke-test step or a readiness probe.

### Exemplars (optional)

Where the **knowledge base** grounds replies in agency facts (the *what*), the **exemplars** layer sets the agency's voice — the *how*. It's a small, opt-in companion module that reads a curator-managed Drive folder where each Google Doc is one example reply, then injects all of them as a separate `EXAMPLE RESPONSES` block in the LLM prompt. The framing tells the model to mirror style and structure but to defer to the KB whenever an example contradicts it on facts.

**Why this is separate from the KB.** The KB is *required*, *fail-loud*, and chunked + retrieved per email (RAG). Exemplars are *optional*, *graceful*, and **raw-injected without retrieval** — the curator-led pool is small enough (10–50 docs) that the entire set fits in the LLM context window, so we trade RAG's per-request latency and operational complexity for a much simpler shape. KB failures abort drafting; exemplar failures degrade silently to KB-only.

**Curator contract.**

1. Create a dedicated Drive folder; share it with the same service account that has `KB_RAG_FOLDER_IDS` access (already in scope, no new OAuth grant needed).
2. **One Doc per exemplar.** Title each Doc with what it covers ("Refund policy for weather cancellations", "Altitude sickness preparation"); the title becomes the example heading in the prompt.
3. Each Doc is one self-contained reply — Q+A pair, template, or a representative thread excerpt. Use Markdown freely; no schema.
4. **Anonymize while writing.** Use placeholders like `<guest>`, `<tour-name>`, `<date>`. The curator's eyeball is the primary defense against PII leaks.
5. The runtime regex pass (`exemplars/sanitize.py`) is a tripwire that catches the obvious mechanical leaks the curator might miss — emails, phone numbers, IBANs, Luhn-valid card numbers, booking URLs. Anything it catches is logged at WARNING; an integration tripwire test in CI guards that nothing slips past.

**Configuration.** Set `KB_EXEMPLAR_FOLDER_IDS` to one or more Drive folder IDs or share URLs (comma-separated). Unset = exemplars disabled, no behavior change. The same `drive.readonly` scope already in the OAuth grant covers reads.

**Refresh story.** Exemplars are loaded **once per process**: the first call after a Cloud Run cold start reads Drive (in parallel via a thread pool — ~1s for 30 Docs), sanitizes, and caches in memory. The FastAPI app installs a `lifespan` warm-up hook that runs this load synchronously during container startup, **before** the instance accepts traffic, so the first user request pays 0ms for exemplars. Curator edits to a Doc become visible after the next process cold start; for an immediate refresh, trigger a new Cloud Run revision rollout.

For zero cold-start spikes between refreshes, deploy the API with `**--min-instances=1`** (~$5–10/month). One always-warm instance means the warm-up runs once at deploy time and every subsequent request reuses the cached pool. Without `min-instances`, Cloud Run still calls the warm-up on each cold start, so cold-start latency for the *first* user request is unaffected by exemplars; only the container boot itself is slightly longer.

**Sanity check what the agent sees.**

```bash
uv run python -m wayonagio_email_agent.cli exemplar-list
```

This prints the Drive Doc title, ID, and a short body preview (post-sanitization) for each exemplar in the order the runtime will see them. Useful for confirming a curator's edit landed and that PII redaction did its job.

**Failure behavior.** Every exemplar failure path returns an empty list: feature disabled, Drive unreachable, folder empty, Doc unreadable, Doc is empty after extraction. Drafts simply omit the `EXAMPLE RESPONSES` block. Failures are logged at WARNING and cached for the process lifetime so a Drive outage doesn't trigger per-request retries.

For the full design rationale (why raw injection over RAG, why one Doc per exemplar, the migration path if the pool ever outgrows the context window), see `[src/wayonagio_email_agent/exemplars/README.md](src/wayonagio_email_agent/exemplars/README.md)`.

## Gmail Add-on setup

The Add-on lives in `addon/`. It adds **two language-specific draft buttons** inside Gmail when viewing an email:

- **Draft in Italian**
- **Draft in Spanish**

1. Go to [script.google.com](https://script.google.com) and create a new project.
2. Copy the contents of `addon/Code.gs` and `addon/appsscript.json` into the project.
3. In the Apps Script editor, go to **Project Settings → Script Properties** and add:
  - `BACKEND_URL` — your server URL, e.g. `https://your-server.example.com`
  - `BEARER_TOKEN` — the same value as `AUTH_BEARER_TOKEN` in your `.env`
4. **Allowlists in `appsscript.json` (required to deploy).** Google Workspace add-ons that call `UrlFetchApp` must declare `urlFetchWhitelist` with an **HTTPS URL prefix** for each host you call (trailing path required, e.g. `https://example.com/`). The template includes `https://*.a.run.app/` for **Cloud Run** (`BACKEND_URL` on `*.a.run.app`). If you use a **custom domain** (or `ngrok`, etc.) instead, add another prefix to `urlFetchWhitelist` for that host, save the manifest, then deploy. The manifest also allowlists `https://mail.google.com/` for the **Open thread** link on the success card.
5. Click **Deploy → New Deployment** → type **Google Workspace Add-on**.
6. Install the Add-on for your Workspace domain via the Admin console, or install it for yourself via the deployment URL.

When a staff member opens an email in Gmail, the Add-on panel appears on the right. Clicking one of the language buttons calls `POST /draft-reply` on the backend with `message_id` and `language` (`it` or `es`), and a draft appears in the Gmail thread.

## Recommended deployment: Cloud Run + Gemini

For the agency use case (seasonal traffic, no desire to maintain a server, small budget), the recommended deployment is:

- **Compute**: [Google Cloud Run](https://cloud.google.com/run) — serverless, scales to zero when idle, pay-per-request.
- **LLM**: Google Gemini API (`gemini/gemini-2.5-flash`) — pay-per-token, no cold-start model loading, no hardware.
- **Secrets**: Google [Secret Manager](https://cloud.google.com/secret-manager) — avoids baking tokens into the image or Cloud Run env vars.

**Why this over self-hosted?** A dedicated box running Ollama costs 600–2000 up front plus monthly electricity and your time on OS/LLM upkeep. On Cloud Run + Gemini Flash, a travel-agency workload (tens to low hundreds of drafts per month) typically costs single-digit dollars per month, drops near zero during the off-season, and has no hardware to babysit. Privacy posture is comparable because the client email data is already in Gmail (Google) — Gemini's paid tier does not train on your API inputs.

### 1. One-time Google Cloud setup

```bash
# Install and log in to gcloud (once, on your laptop).
# https://cloud.google.com/sdk/docs/install

gcloud auth login
# Use the project ID string (e.g. "wayonagio-prod"), NOT the numeric project number.
PROJECT_ID="YOUR_PROJECT_ID"
gcloud config set project "$PROJECT_ID"
# If you accidentally set a numeric project number, re-run the line above with the ID.

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com
```

> **Billing required:** these APIs will fail to enable unless the project is linked
> to an active Cloud Billing account. If you see `FAILED_PRECONDITION` about
> billing, link billing in the Google Cloud Console first, then re-run
> `gcloud services enable`.

Create an Artifact Registry repo to hold the container image:

```bash
gcloud artifacts repositories create wayonagio \
  --project="$PROJECT_ID" \
  --repository-format=docker \
  --location=us-central1
```

Create the GCS bucket that will hold the knowledge-base artifact. The API service and the ingest Job both read/write `kb_index.sqlite` here, so this is a hard prerequisite:

```bash
gcloud storage buckets create gs://wayonagio-kb \
  --location=us-central1 \
  --uniform-bucket-level-access
```

### 2. Store secrets in Secret Manager

The container must **not** contain `credentials.json`, `token.json`, `AUTH_BEARER_TOKEN`, or `GEMINI_API_KEY`. Put them in Secret Manager instead.

```bash
# OAuth client secrets (download from Google Cloud Console)
gcloud secrets create gmail-credentials --data-file=credentials.json

# OAuth token (generate locally first via `uv run python -m wayonagio_email_agent.cli auth`)
gcloud secrets create gmail-token --data-file=token.json

# Bearer token for the API
printf '%s' "$(openssl rand -base64 32)" \
  | gcloud secrets create auth-bearer-token --data-file=-

# Gemini API key (from https://aistudio.google.com/app/apikey)
printf '%s' "YOUR_GEMINI_KEY" \
  | gcloud secrets create gemini-api-key --data-file=-
```

> **Important — `token.json` bootstrapping.** Gmail OAuth for a "Desktop app" client uses a loopback redirect and requires a browser. Cloud Run has no browser. Always run `cli auth` **once on your laptop** (or any machine with a browser), using the same `credentials.json` you uploaded to Secret Manager, to generate `token.json`, then upload that file to Secret Manager as shown above. Cloud Run then reads the refreshed token from Secret Manager at startup.

Create a dedicated service account for the Cloud Run service and grant it access to each secret:

```bash
gcloud iam service-accounts create wayonagio-run \
  --project="$PROJECT_ID" \
  --display-name="Wayonagio Email Agent (Cloud Run)"

SA="wayonagio-run@${PROJECT_ID}.iam.gserviceaccount.com"

for secret in gmail-credentials gmail-token auth-bearer-token gemini-api-key; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

Grant the service account access to the KB bucket. The ingest Job writes `kb_index.sqlite`; the API service reads it at cold start. One service account covers both because neither role conflicts with the other — and running one service account is easier to audit than two.

```bash
gcloud storage buckets add-iam-policy-binding gs://wayonagio-kb \
  --member="serviceAccount:${SA}" \
  --role="roles/storage.objectAdmin"
```

### 3. Test the image locally (optional)

Before pushing, you can sanity-check the container locally:

```bash
docker build -t wayonagio-email-agent:dev .
docker run --rm -p 8080:8080 \
  --env-file .env \
  -v "$PWD/credentials.json:/app/credentials.json:ro" \
  -v "$PWD/token.json:/app/token.json:ro" \
  wayonagio-email-agent:dev
curl http://localhost:8080/healthz || curl http://localhost:8080/docs
```

### 4. Build and push the image

```bash
gcloud builds submit \
  --project="$PROJECT_ID" \
  --tag "us-central1-docker.pkg.dev/${PROJECT_ID}/wayonagio/email-agent:latest"
```

If `gcloud builds submit` fails with `storage.objects.get` on
`<project-number>-compute@developer.gserviceaccount.com`, grant that service
account read access to the Cloud Build staging bucket, then retry:

```bash
PROJECT_ID="wayonagio-agente-ia-email"
PROJECT_NUMBER="663273531878"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
BUCKET="gs://${PROJECT_ID}_cloudbuild"

gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/storage.objectViewer" \
  --project="$PROJECT_ID"
```

If the build uploads successfully but then fails with `denied: artifactregistry.repositories.uploadArtifacts` (and/or says the same service
account cannot write Cloud Logging logs), grant Artifact Registry writer on the
repo and Logs Writer on the project:

```bash
PROJECT_ID="wayonagio-agente-ia-email"
PROJECT_NUMBER="663273531878"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud artifacts repositories add-iam-policy-binding wayonagio \
  --project="$PROJECT_ID" \
  --location="us-central1" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/artifactregistry.writer"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/logging.logWriter"
```

### 5. Deploy to Cloud Run

```bash
SA="wayonagio-run@${PROJECT_ID}.iam.gserviceaccount.com"
IMG="us-central1-docker.pkg.dev/${PROJECT_ID}/wayonagio/email-agent:latest"
```

Replace the `<rag-folder-ids>` placeholder with the comma-separated Drive folder IDs (or share URLs) that hold the agency's tour descriptions, FAQs, and templates. The KB is required — the service will return `503 Knowledge base unavailable` on every draft until `KB_RAG_FOLDER_IDS` is set and a `kb-ingest` run has published `kb_index.sqlite` to `gs://wayonagio-kb` (step 6 below).

If you set multiple RAG folders manually with `gcloud run deploy`, use a custom
`--set-env-vars` delimiter so commas inside the value are preserved. Example:
`--set-env-vars="^@^KB_RAG_FOLDER_IDS=id1,id2@LLM_MODEL=gemini/gemini-2.5-flash"`.
The same pattern applies to `KB_EXEMPLAR_FOLDER_IDS` if you enable exemplars.
If you are not using exemplars yet, set `KB_EXEMPLAR_FOLDER_IDS=` (empty).

```bash
gcloud run deploy wayonagio-email-agent \
  --project="$PROJECT_ID" \
  --image="$IMG" \
  --region=us-central1 \
  --platform=managed \
  --service-account="$SA" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --cpu=1 --memory=512Mi \
  --set-env-vars="^@^LLM_MODEL=gemini/gemini-2.5-flash@GMAIL_CREDENTIALS_PATH=/secrets/gmail-credentials/credentials.json@GMAIL_TOKEN_PATH=/secrets/gmail-token/token.json@SCANNER_ENABLED=false@LOG_LEVEL=INFO@KB_GCS_URI=gs://wayonagio-kb@KB_EMBEDDING_MODEL=gemini/gemini-embedding-001@KB_RAG_FOLDER_IDS=<rag-folder-ids>@KB_EXEMPLAR_FOLDER_IDS=<exemplar-folder-ids-optional>" \
  --set-secrets=AUTH_BEARER_TOKEN=auth-bearer-token:latest,GEMINI_API_KEY=gemini-api-key:latest \
  --set-secrets=/secrets/gmail-credentials/credentials.json=gmail-credentials:latest,/secrets/gmail-token/token.json=gmail-token:latest
```

Notes:

- `--allow-unauthenticated` is required because the Gmail Add-on can't attach a Google-IAM identity token. **Authentication is still enforced**: every request must carry `Authorization: Bearer $AUTH_BEARER_TOKEN`.
- `--min-instances=0` means the service scales to zero when idle and you pay nothing; cold-start adds ~~1–2s on the first request. Set `--min-instances=1` (~~5–10/month) if you want instant response at all times. If you've populated `KB_EXEMPLAR_FOLDER_IDS`, `min-instances=1` also means the exemplar warm-up runs once at deploy time and every subsequent request reuses the cached pool (see [Exemplars](#exemplars-optional)).
- Cloud Run mounts each file secret as read-only at the given path, so the Gmail token refresher cannot write back. To rotate `token.json`, re-run `cli auth` locally and add a new version: `gcloud secrets versions add gmail-token --data-file=token.json`.

After deploy, copy the HTTPS URL Cloud Run prints (e.g. `https://wayonagio-email-agent-xxxxx-uc.a.run.app`) into the Apps Script `BACKEND_URL` Script Property, and set `BEARER_TOKEN` to the same value you stored in `auth-bearer-token`.

#### If public invoker is blocked (Google Cloud organization policy)

`gcloud run deploy` with `--allow-unauthenticated` tries to let anyone **reach** the HTTPS URL; your app still requires `Authorization: Bearer` on `/draft-reply`. Some **Workspace / Cloud organizations** also enforce **“Domain-restricted sharing”** (`iam.allowedPolicyMemberDomains`), which only allows IAM members from your org’s **customer id** (e.g. a value like `C0…`). The principal `allUsers` is **not** in that set, so a follow-up step can fail with errors such as:

- `FAILED_PRECONDITION: One or more users named in the policy do not belong to a permitted customer…`
- Policy modification failed when setting `allUsers` as `roles/run.invoker`

**Roles:** *Organization Administrator* is not the same as *Organization Policy Administrator*. To edit org policies in the console you need `roles/orgpolicy.policyAdmin` (or another role that includes `orgpolicy.`* on the **organization**). Grant it (to yourself or an ops group) in **Organization → IAM** if the **Organization policies** UI is read-only.

**Project-only fix (recommended):** relax this constraint for **one project** (the one that hosts Cloud Run), not the whole org.

1. In the top bar, select **project** `PROJECT_ID` (not the organization).
2. **IAM & Admin → Organization policies** (Políticas de la organización).
3. Open **Domain-restricted sharing** / `iam.allowedPolicyMemberDomains`.
4. **Policy source / Fuente de las políticas:** choose **Override parent policy** (*Anular política del elemento superior*).
5. **Policy application / Aplicación de la política:** choose **Replace** (*Reemplazar* — *ignore the parent and use these rules*), not “merge with parent,” so this project is not still bound by the org’s allow-list alone.
6. Add **one** new rule: **Allow all** (*Permitir todo*). You usually **do not** need a **condition** when the policy is already scoped to this project.
7. Save and wait a minute for propagation.

Then grant public invoker on the service (if deploy did not already succeed):

```bash
gcloud run services add-iam-policy-binding wayonagio-email-agent \
  --project="$PROJECT_ID" \
  --region=us-central1 \
  --member="allUsers" \
  --role="roles/run.invoker"
```

**Why this is still safe for this app:** `allUsers` + `run.invoker` only exposes the **URL** at the Google front end; unauthenticated clients get **401** on `/draft-reply` without the correct bearer token. Keep `LOG_LEVEL=INFO` in production and rotate `auth-bearer-token` on any leak (see [Security and privacy posture](#security-and-privacy-posture)).

If the **condition** editor only offers **tag**-based rules for this constraint, prefer the **project replace + Allow all** path above instead of CEL on the organization.

### 6. Populate the knowledge base

The service is deployed but will 503 on every draft until the KB index exists. Deploy the ingest Job, run it once manually, then let Cloud Scheduler own the yearly cadence.

```bash
# Create the Job (reuses the same image as the API).
gcloud run jobs create wayonagio-kb-ingest \
  --project="$PROJECT_ID" \
  --image="$IMG" \
  --region=us-central1 \
  --service-account="$SA" \
  --command="python" \
  --args="-m,wayonagio_email_agent.cli,kb-ingest" \
  --set-env-vars="^@^LLM_MODEL=gemini/gemini-2.5-flash@KB_GCS_URI=gs://wayonagio-kb@KB_EMBEDDING_MODEL=gemini/gemini-embedding-001@KB_RAG_FOLDER_IDS=<rag-folder-ids>@GMAIL_CREDENTIALS_PATH=/secrets/gmail-credentials/credentials.json@GMAIL_TOKEN_PATH=/secrets/gmail-token/token.json" \
  --set-secrets=AUTH_BEARER_TOKEN=auth-bearer-token:latest,GEMINI_API_KEY=gemini-api-key:latest \
  --set-secrets=/secrets/gmail-credentials/credentials.json=gmail-credentials:latest,/secrets/gmail-token/token.json=gmail-token:latest

# Run it once NOW, synchronously, so the next draft request has a KB to ground in.
gcloud run jobs execute wayonagio-kb-ingest --project="$PROJECT_ID" --region=us-central1 --wait
```

If `--wait` shows no inline progress, inspect the latest execution and read its
logs in a second terminal.

`gcloud run jobs executions list --format='value(name)'` returns a **full**
resource name (`…​/executions/wayonagio-kb-ingest-xxxxx`). Log entries for Cloud
Run jobs are tagged with the **short** execution id only, and the field is
`labels.execution_name` (see the “Log entry fields for jobs” table in
[Cloud Run logging](https://cloud.google.com/run/docs/logging)) — not
`run.googleapis.com/execution_name`. `textPayload` is also often empty for lines
emitted as JSON, so the example below includes `jsonPayload.message`.

Use **newest first** (`--order=desc`, the default) and a **tight** window
(`--freshness=1h`) so the query returns quickly. **`--order=asc` with a long
`--freshness` window** can take a long time: the API has to satisfy “oldest
first” over the whole range. If the terminal **looks stuck** with no output,
`gcloud` may have opened a **pager** (`less`) — press `q` to exit, or run with
`PAGER=cat` (or append `| cat`) so output prints immediately.

```bash
EXECUTION_PATH="$(gcloud run jobs executions list \
  --project="$PROJECT_ID" \
  --region=us-central1 \
  --job=wayonagio-kb-ingest \
  --limit=1 \
  --format='value(name)')"
# Last path segment, e.g. wayonagio-kb-ingest-abc12 — must match log labels
EXECUTION_ID="${EXECUTION_PATH##*/}"

PAGER=cat gcloud logging read \
  "resource.type=cloud_run_job \
   AND resource.labels.job_name=wayonagio-kb-ingest \
   AND labels.execution_name=\"${EXECUTION_ID}\"" \
  --project="$PROJECT_ID" \
  --limit=200 \
  --order=desc \
  --freshness=1h \
  --format="table(timestamp,severity,textPayload,jsonPayload.message)"
```

If that still returns nothing, logs may be delayed by a minute or the filter
may be too strict — read **recent** lines for the job without the execution
clause, then narrow down:

```bash
PAGER=cat gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=wayonagio-kb-ingest" \
  --project="$PROJECT_ID" \
  --limit=100 \
  --order=desc \
  --freshness=1h \
  --format=json
```

To read a single execution in **chronological** order, use the execution’s
**Logs** tab in the Cloud Run console, or add a **narrow** `timestamp` range
from `gcloud run jobs executions describe` and only then use `--order=asc` on
that small window (wide `asc` + long `freshness` stays slow).

Schedule future ingest runs. The agency refreshes its tour content roughly once a year, so a yearly cron is correct; run the Job manually via `gcloud run jobs execute` for any out-of-cycle edits.

```bash
gcloud scheduler jobs create http wayonagio-kb-ingest-yearly \
  --project="$PROJECT_ID" \
  --location=us-central1 \
  --schedule="15 4 15 1 *" \
  --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/wayonagio-kb-ingest:run" \
  --http-method=POST \
  --oauth-service-account-email="$SA"
```

Verify the KB the service will see on its next cold start:

```bash
# Locally, pointing at the same GCS bucket the service reads:
KB_GCS_URI=gs://wayonagio-kb \
KB_RAG_FOLDER_IDS=<rag-folder-ids> \
  uv run python -m wayonagio_email_agent.cli kb-doctor
```

`kb-doctor` exits non-zero on any hard failure (missing artifact, empty index, embedding-model mismatch), so this is also a good command to run as a deploy smoke-test step. Expect output like:

```
KB status: HEALTHY

Config:
  RAG folders configured:  1
  Embedding model:         gemini/gemini-embedding-001
  Top-K:                   4
  Artifact destination:    gs://wayonagio-kb/kb_index.sqlite

Index:
  Artifact available:      yes
  Loaded:                  yes
  Ingested at:             2026-04-18T10:30:21+00:00 (<1h ago)
  Chunks:                  247
  ...
```

If `kb-doctor` says `UNHEALTHY`, fix what it reports before wiring the Add-on to the service.

### 7. Update / rotate

- **New code**: re-run steps 4 and 5. Cloud Run does zero-downtime traffic swap.
- **Rotate bearer token**: `printf '%s' "$(openssl rand -base64 32)" | gcloud secrets versions add auth-bearer-token --data-file=-`, then `gcloud run services update wayonagio-email-agent --region=us-central1 --set-secrets=AUTH_BEARER_TOKEN=auth-bearer-token:latest`, then update the Apps Script `BEARER_TOKEN`.
- **Rotate Gemini key**: `gcloud secrets versions add gemini-api-key --data-file=-`, then redeploy/update the service so it picks up the new version.
- **Refresh Gmail token** (if the OAuth refresh token is ever revoked): `uv run python -m wayonagio_email_agent.cli auth` locally, then `gcloud secrets versions add gmail-token --data-file=token.json`, then update the service.

### Optional automation scripts

If you prefer one-command workflows instead of running each `gcloud` step manually,
use the scripts in `scripts/cloud/`.

Prereqs:

- Run from any directory; scripts resolve repo root automatically.
- `gcloud`, `docker`, and `openssl` installed.
- `credentials.json` and `token.json` available (or set `CREDENTIALS_FILE` / `TOKEN_FILE`).

```bash
# 1) Full deployment from scratch (infra + secrets + build + deploy + ingest)
PROJECT_ID="your-project-id" \
RAG_FOLDER_IDS="drive-folder-id-1,drive-folder-id-2" \
EXEMPLAR_FOLDER_IDS="optional-exemplar-folder-id-1,optional-exemplar-folder-id-2" \
GEMINI_API_KEY="your-gemini-api-key" \
scripts/cloud/deploy_from_scratch.sh

# (Optional) if/when you decide a cadence, add scheduler separately.
# The script intentionally does not create Cloud Scheduler jobs.

# 2) Update existing deployment (new code/dependencies + redeploy)
PROJECT_ID="your-project-id" \
RAG_FOLDER_IDS="drive-folder-id-1,drive-folder-id-2" \
EXEMPLAR_FOLDER_IDS="optional-exemplar-folder-id-1,optional-exemplar-folder-id-2" \
scripts/cloud/update.sh

# Optional on update: refresh secrets and run KB ingest immediately
PROJECT_ID="your-project-id" \
RAG_FOLDER_IDS="drive-folder-id-1,drive-folder-id-2" \
TOKEN_FILE="./token.json" \
RUN_KB_INGEST=true \
scripts/cloud/update.sh

# 3) Tear down service/job/scheduler (non-destructive defaults)
PROJECT_ID="your-project-id" \
scripts/cloud/teardown.sh

# Tear down everything (service + scheduler + job + repo + bucket + secrets + SA)
PROJECT_ID="your-project-id" \
DELETE_REPOSITORY=true \
DELETE_BUCKET=true \
DELETE_SECRETS=true \
DELETE_SERVICE_ACCOUNT=true \
FORCE=true \
scripts/cloud/teardown.sh
```

Use `--help` on each script for all environment variables and defaults:
`scripts/cloud/deploy_from_scratch.sh --help`, `scripts/cloud/update.sh --help`,
`scripts/cloud/teardown.sh --help`.

## Server deployment

For fully self-hosted deployments (typically Ollama + your own hardware), run the FastAPI server behind a TLS-terminating reverse proxy. Cloud Run is simpler for most cases — prefer [Cloud Run + Gemini](#recommended-deployment-cloud-run--gemini) unless you have a specific reason to self-host.

The server must be reachable from the internet (the Gmail Add-on calls it from Google's servers).

**Option A: Caddy (recommended)**

```
your-server.example.com {
    reverse_proxy localhost:8000
}
```

Caddy handles HTTPS/TLS automatically via Let's Encrypt.

**Option B: Nginx**

```nginx
server {
    listen 443 ssl;
    server_name your-server.example.com;
    # ... TLS config ...
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Run uvicorn on a private port and let the proxy handle public HTTPS. Never expose port 8000 directly.

### Running as a service (systemd example)

```ini
[Unit]
Description=Wayonagio Email Agent API
After=network.target

[Service]
WorkingDirectory=/path/to/wayonagio-email-agent
ExecStart=/path/to/.venv/bin/uvicorn wayonagio_email_agent.api:app --host 127.0.0.1 --port 8000
EnvironmentFile=/path/to/wayonagio-email-agent/.env
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

For a phased rollout, keep the API service enabled first and run the scanner as a separate optional service later. Example scanner unit:

```ini
[Unit]
Description=Wayonagio Email Agent Scanner
After=network.target

[Service]
WorkingDirectory=/path/to/wayonagio-email-agent
ExecStart=/path/to/.venv/bin/python -m wayonagio_email_agent.cli scan --interval 1800
EnvironmentFile=/path/to/wayonagio-email-agent/.env
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Leave `SCANNER_ENABLED=false` until you want to activate automatic drafting, then change it to `true` and start the scanner service.

## Tests

```bash
uv run pytest
```

All tests use mocked Gmail and Ollama calls — no real credentials or running services needed. Coverage includes API auth, scanner behavior, MIME/threading draft construction, and Gmail payload parsing.

## Local Testing

End-to-end Phase 1 setup on your own machine lives in a dedicated guide so this README can stay focused on overview and reference material:

**→ `[docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md)`**

It walks through OAuth credentials, picking your LLM (Gemini **or** Ollama — step 2 forks), configuring `.env`, `cli auth` + `kb-ingest` + `kb-doctor`, testing via CLI and API, and deploying the Gmail Add-on against your local machine through `ngrok`/`cloudflared`.

## Architecture

```
src/wayonagio_email_agent/
  gmail_client.py     # Gmail + Drive API: OAuth, list/get/draft/dedup
  llm/client.py       # LiteLLM-backed LLM: detect_language, generate_reply, is_travel_related
  agent.py            # Orchestration: manual flow + scanner loop
  api.py              # FastAPI: POST /draft-reply
  cli.py              # CLI: auth, list, draft-reply, scan, scan-once, kb-ingest, kb-search, kb-doctor, exemplar-list
  state.py            # SQLite dedup state for scanner
  kb/                 # Knowledge base (required, KB_RAG_FOLDER_IDS)
    config.py         # Env-driven KB config + Drive URL parsing
    drive.py          # Drive wrapper: list folders, export Docs, download files
    extract.py        # MIME-dispatched text extraction (PDF / Docs / txt / md)
    chunk.py          # Paragraph-aware chunker with overlap
    embed.py          # LiteLLM-backed batched embeddings
    store.py          # SQLite vector store + numpy cosine top-k
    artifact.py       # GCS + local artifact I/O
    retrieve.py       # Runtime: retrieve() for llm/client
    ingest.py         # End-to-end ingest pipeline (kb-ingest)
    doctor.py         # Health report builder behind `cli kb-doctor`
  exemplars/          # Curator-led example replies (optional, KB_EXEMPLAR_FOLDER_IDS)
    config.py         # Env-driven exemplar config (parallel to kb.config)
    sanitize.py       # PII tripwire: BOOKING_URL/email/IBAN/Luhn-card/phone passes
    source.py         # Drive-folder source, parallel ThreadPoolExecutor reads
    loader.py         # Process-level cache, double-checked lock, never-raises contract
    prompt.py         # Format the EXAMPLE RESPONSES block with KB-precedence framing
addon/
  Code.gs             # Apps Script: Gmail contextual Add-on
  appsscript.json
Dockerfile            # Cloud Run-ready container image
tests/
  test_llm.py
  test_agent.py
  test_api.py
  test_kb_*.py
```

For deeper technical context:

- `[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)` — system-level technical writeup: how every module wires together, the cross-cutting concerns, the load-bearing invariants, and the rationale for the shape of the codebase.
- `[src/wayonagio_email_agent/kb/README.md](src/wayonagio_email_agent/kb/README.md)` — deep dive on the knowledge base specifically.
- `[AGENTS.md](AGENTS.md)` — quick orientation for AI coding agents working in this repo.

