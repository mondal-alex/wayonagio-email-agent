# Wayonagio Email Agent

A **draft-only** email response agent for a Cusco (Peru) travel agency. It connects to Gmail via the Gmail API and uses an LLM to generate multilingual draft replies (Italian, Spanish, English). Staff trigger drafts via a Gmail Add-on button; an automatic scanner creates drafts for new travel-related emails.

**The agent never sends email.** It only calls `drafts.create`. Sending is always done manually by staff in Gmail.

LLM calls go through [LiteLLM](https://docs.litellm.ai/), which lets you swap providers without code changes. Two supported setups:

- **Google Gemini API** (recommended for production) — pay-per-use, no hardware to maintain. See [Recommended deployment: Cloud Run + Gemini](#recommended-deployment-cloud-run--gemini).
- **Self-hosted Ollama** (recommended for local dev or fully offline deployments) — no external API calls, runs on your own box.

## Prerequisites

- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with the Gmail API enabled and OAuth 2.0 credentials downloaded
- **One** of:
  - A [Google AI Studio API key](https://aistudio.google.com/app/apikey) for Gemini, **or**
  - [Ollama](https://ollama.com) running locally or on the server (`ollama serve`)

## Google Cloud / Gmail setup

1. Go to [Google Cloud Console](https://console.cloud.google.com) → **APIs & Services** → **Library**.
2. Enable the **Gmail API**.
3. Go to **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth client ID**.
   - Application type: **Desktop app**
4. Download the JSON file and save it as `credentials.json` in the project root.
5. Go to **OAuth consent screen / Data access** and add these scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
6. Go to **OAuth consent screen / Audience** and add the Gmail account as a test user (while in testing mode).

## Installation

```bash
uv sync
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Scope | Description |
|---|---|---|
| `GMAIL_CREDENTIALS_PATH` | always | Path to `credentials.json` (OAuth client secrets) |
| `GMAIL_TOKEN_PATH` | always | Path for persisted OAuth token (written by `cli auth`) |
| `LLM_MODEL` | always | LiteLLM model string: `gemini/gemini-2.5-flash`, `ollama/llama3.2`, etc. |
| `GEMINI_API_KEY` | Gemini only | Google AI Studio API key. Required when `LLM_MODEL` starts with `gemini/`. |
| `OLLAMA_BASE_URL` | Ollama only | Ollama server URL (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | Ollama only (legacy) | Back-compat: if `LLM_MODEL` is unset, we use `ollama/<OLLAMA_MODEL>`. |
| `OLLAMA_KEEP_ALIVE` | Ollama only | How long Ollama keeps the model loaded (e.g. `5m`, `1h`, `-1`). Default `1h`. |
| `AUTH_BEARER_TOKEN` | always | Bearer token for API authentication |
| `SCANNER_ENABLED` | always | Feature flag for automatic scanner (`false` by default) |
| `SCANNER_STATE_DB` | always | SQLite DB path for scanner dedup state (default: `scanner_state.db`) |
| `LOG_LEVEL` | always | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

### Choosing an LLM provider

Set `LLM_MODEL` to a LiteLLM `<provider>/<model>` string.

| Use case | Set `LLM_MODEL` to | Also set |
|---|---|---|
| Production on Cloud Run (recommended) | `gemini/gemini-2.5-flash` | `GEMINI_API_KEY` |
| Local development, fully offline | `ollama/llama3.2` | `OLLAMA_BASE_URL`, `OLLAMA_KEEP_ALIVE` |
| Other LiteLLM-supported provider | see the [LiteLLM docs](https://docs.litellm.ai/docs/providers) | provider-specific env vars |

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
- OAuth scopes are `gmail.readonly` + `gmail.compose` only. **`gmail.send` is intentionally absent**, so the server physically cannot send mail even if compromised.
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

```bash
# Run continuously, scanning every 30 minutes
uv run python -m wayonagio_email_agent.cli scan --interval 1800

# Test classification without creating any drafts
uv run python -m wayonagio_email_agent.cli scan --dry-run
```

Scanner behavior:
- It polls unread mail with `is:unread`.
- Each message ID is recorded in the local SQLite state DB after a final scanner outcome: `drafted`, `non_travel`, or `thread_has_draft`.
- This prevents repeated Ollama classification of the same unread non-travel messages across scans and restarts.
- If Gmail draft lookup fails for a thread, the scanner skips that message for the current iteration instead of assuming it is safe to draft.
- `--dry-run` does not create drafts and does not persist scanner state.
- If `SCANNER_ENABLED=false`, the `scan` command exits immediately with a clear error instead of starting.

### CLI (admin)

```bash
# List recent unread emails
uv run python -m wayonagio_email_agent.cli list

# Create a draft for a specific message
uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
```

## Gmail Add-on setup

The Add-on lives in `addon/`. It adds **two language-specific draft buttons** inside Gmail when viewing an email:
- **Draft in Italian**
- **Draft in Spanish**

1. Go to [script.google.com](https://script.google.com) and create a new project.
2. Copy the contents of `addon/Code.gs` and `addon/appsscript.json` into the project.
3. In the Apps Script editor, go to **Project Settings → Script Properties** and add:
   - `BACKEND_URL` — your server URL, e.g. `https://your-server.example.com`
   - `BEARER_TOKEN` — the same value as `AUTH_BEARER_TOKEN` in your `.env`
4. Click **Deploy → New Deployment** → type **Google Workspace Add-on**.
5. Install the Add-on for your Workspace domain via the Admin console, or install it for yourself via the deployment URL.

When a staff member opens an email in Gmail, the Add-on panel appears on the right. Clicking one of the language buttons calls `POST /draft-reply` on the backend with `message_id` and `language` (`it` or `es`), and a draft appears in the Gmail thread.

## Recommended deployment: Cloud Run + Gemini

For the agency use case (seasonal traffic, no desire to maintain a server, small budget), the recommended deployment is:

- **Compute**: [Google Cloud Run](https://cloud.google.com/run) — serverless, scales to zero when idle, pay-per-request.
- **LLM**: Google Gemini API (`gemini/gemini-2.5-flash`) — pay-per-token, no cold-start model loading, no hardware.
- **Secrets**: Google [Secret Manager](https://cloud.google.com/secret-manager) — avoids baking tokens into the image or Cloud Run env vars.

**Why this over self-hosted?** A dedicated box running Ollama costs \$600–\$2000 up front plus monthly electricity and your time on OS/LLM upkeep. On Cloud Run + Gemini Flash, a travel-agency workload (tens to low hundreds of drafts per month) typically costs single-digit dollars per month, drops near zero during the off-season, and has no hardware to babysit. Privacy posture is comparable because the client email data is already in Gmail (Google) — Gemini's paid tier does not train on your API inputs.

### 1. One-time Google Cloud setup

```bash
# Install and log in to gcloud (once, on your laptop).
# https://cloud.google.com/sdk/docs/install

gcloud auth login
gcloud config set project YOUR_PROJECT_ID

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com
```

Create an Artifact Registry repo to hold the container image:

```bash
gcloud artifacts repositories create wayonagio \
  --repository-format=docker \
  --location=us-central1
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
  --display-name="Wayonagio Email Agent (Cloud Run)"

SA="wayonagio-run@$(gcloud config get-value project).iam.gserviceaccount.com"

for secret in gmail-credentials gmail-token auth-bearer-token gemini-api-key; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

### 3. Build and push the image

```bash
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/$(gcloud config get-value project)/wayonagio/email-agent:latest
```

### 4. Deploy to Cloud Run

```bash
SA="wayonagio-run@$(gcloud config get-value project).iam.gserviceaccount.com"
IMG="us-central1-docker.pkg.dev/$(gcloud config get-value project)/wayonagio/email-agent:latest"

gcloud run deploy wayonagio-email-agent \
  --image="$IMG" \
  --region=us-central1 \
  --platform=managed \
  --service-account="$SA" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --cpu=1 --memory=512Mi \
  --set-env-vars=LLM_MODEL=gemini/gemini-2.5-flash,GMAIL_CREDENTIALS_PATH=/secrets/credentials.json,GMAIL_TOKEN_PATH=/secrets/token.json,SCANNER_ENABLED=false,LOG_LEVEL=INFO \
  --set-secrets=AUTH_BEARER_TOKEN=auth-bearer-token:latest,GEMINI_API_KEY=gemini-api-key:latest \
  --set-secrets=/secrets/credentials.json=gmail-credentials:latest,/secrets/token.json=gmail-token:latest
```

Notes:
- `--allow-unauthenticated` is required because the Gmail Add-on can't attach a Google-IAM identity token. **Authentication is still enforced**: every request must carry `Authorization: Bearer $AUTH_BEARER_TOKEN`.
- `--min-instances=0` means the service scales to zero when idle and you pay nothing; cold-start adds ~1–2s on the first request. Set `--min-instances=1` (~\$5–10/month) if you want instant response at all times.
- Cloud Run mounts each file secret as read-only at the given path, so the Gmail token refresher cannot write back. To rotate `token.json`, re-run `cli auth` locally and add a new version: `gcloud secrets versions add gmail-token --data-file=token.json`.

After deploy, copy the HTTPS URL Cloud Run prints (e.g. `https://wayonagio-email-agent-xxxxx-uc.a.run.app`) into the Apps Script `BACKEND_URL` Script Property, and set `BEARER_TOKEN` to the same value you stored in `auth-bearer-token`.

### 5. Update / rotate

- **New code**: re-run steps 3 and 4. Cloud Run does zero-downtime traffic swap.
- **Rotate bearer token**: `printf '%s' "$(openssl rand -base64 32)" | gcloud secrets versions add auth-bearer-token --data-file=-`, then `gcloud run services update wayonagio-email-agent --region=us-central1 --set-secrets=AUTH_BEARER_TOKEN=auth-bearer-token:latest`, then update the Apps Script `BEARER_TOKEN`.
- **Rotate Gemini key**: `gcloud secrets versions add gemini-api-key --data-file=-`, then redeploy/update the service so it picks up the new version.
- **Refresh Gmail token** (if the OAuth refresh token is ever revoked): `uv run python -m wayonagio_email_agent.cli auth` locally, then `gcloud secrets versions add gmail-token --data-file=token.json`, then update the service.

### 6. Test the image locally (optional)

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

Use this flow to test Phase 1 locally with your own Gmail account. This keeps the rollout manual-first: API + Gmail Add-on, with the scanner disabled.

### 1. Create Gmail OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project, or select an existing test project.
3. Open **APIs & Services → Library**.
4. Search for **Gmail API** and enable it.
5. Open **APIs & Services → OAuth consent screen**.
6. Configure the app as an **External** app if needed.
7. Keep publishing status as **Testing** (do **not** publish for local testing).
8. Add your own Gmail address as a **Test user** while the app is in testing mode.
   - If your account is not listed as a test user, OAuth login will fail with `Error 403: access_denied`.
9. Open **OAuth consent screen / Data access** and add exactly these scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
9. Open **APIs & Services → Credentials**.
10. Click **Create Credentials → OAuth client ID**.
11. Choose **Desktop app**.
12. Download the client credentials JSON file.
13. Save it in this project, for example as `credentials.json`.

### 2. Install and run Ollama locally

1. Install Ollama from [ollama.com](https://ollama.com/download).
2. Start the Ollama server:

```bash
ollama serve
```

3. In a second terminal, download a model, for example:

```bash
ollama pull llama3.2
```

4. Keep note of:
   - `OLLAMA_BASE_URL`, usually `http://localhost:11434`
   - `OLLAMA_MODEL`, for example `llama3.2`

### 3. Configure the app

Copy [`.env.example`](.env.example) to `.env`:

```bash
cp .env.example .env
```

Then set:
- `GMAIL_CREDENTIALS_PATH=credentials.json`
- `GMAIL_TOKEN_PATH=token.json`
- `OLLAMA_BASE_URL=http://localhost:11434`
- `OLLAMA_MODEL=llama3.2`
- `AUTH_BEARER_TOKEN=<your test token>`
- `SCANNER_ENABLED=false`

The scanner should stay off for Phase 1 local testing.

### 4. Install dependencies and authenticate with Gmail

```bash
uv sync
uv run python -m wayonagio_email_agent.cli auth
```

The `auth` command opens a browser, asks you to sign in with your test Gmail account, and writes `token.json`. After that, the app can read messages and create drafts in that account.

If you change scopes later, delete `token.json` and run `cli auth` again so OAuth consent is refreshed with the updated scopes.

If you get `Error 403: access_denied`, verify all of the following:
- OAuth app is still in **Testing** (not published).
- The Gmail account you used to sign in is added under **OAuth consent screen / Audience → Test users**.
- You are using the same project/client that generated your `credentials.json`.

### 5. Test the backend manually from the CLI

List recent unread messages:

```bash
uv run python -m wayonagio_email_agent.cli list
```

```bash
uv run python -m wayonagio_email_agent.cli list --max 50
```

Pick a `message_id`, then create a draft reply:

```bash
uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
```

Open Gmail and confirm:
- a draft was created,
- it appears in the correct thread,
- it was not sent automatically.

### 6. Test the local API directly

Start the API server:

```bash
uv run uvicorn wayonagio_email_agent.api:app --host 127.0.0.1 --port 8000
```

Then call it with `curl`:

```bash
curl -X POST http://127.0.0.1:8000/draft-reply \
  -H "Authorization: Bearer <AUTH_BEARER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"message_id":"<message_id>","language":"it"}'
```

If the response includes a draft ID and a draft appears in Gmail, the API path is working.

### 7. Test the Gmail Add-on against your local machine

Google Apps Script cannot call `localhost` directly, so expose your local API with a public HTTPS tunnel such as `ngrok` or `cloudflared`.

Typical flow:

1. Start the API locally on port `8000`.
2. Start a tunnel that forwards to `http://127.0.0.1:8000`.
3. Copy the public HTTPS URL from the tunnel.
4. Go to [script.google.com](https://script.google.com/).
5. Create a new Apps Script project.
6. Copy in the contents of [`addon/Code.gs`](addon/Code.gs) and [`addon/appsscript.json`](addon/appsscript.json). See [Viewing `appsscript.json` in the editor](#viewing-appsscriptjson-in-the-editor) below if you don't see the manifest file.
7. In **Project Settings → Script Properties**, set:
   - `BACKEND_URL=<your public tunnel URL>`
   - `BEARER_TOKEN=<AUTH_BEARER_TOKEN from .env>`
8. Deploy it as a Google Workspace Add-on. See [Deploying the Add-on for testing](#deploying-the-add-on-for-testing) below for detailed steps.
9. Install it for your test account (done as part of the deployment flow below).
10. Open a Gmail message and click **Draft reply**.

You should see a notification in Gmail and a new draft should appear in the thread.

#### Choosing a tunnel: `cloudflared` vs `ngrok`

| | `cloudflared` (quick tunnel) | `ngrok` |
|---|---|---|
| Signup required | No | Yes (free account) |
| Setup speed | Fastest | Fast after signup |
| Free stable URL | Only with a named tunnel (requires a domain on Cloudflare) | Reserved domains on paid plans |
| Request inspector | No | Yes, at `http://127.0.0.1:4040` |

For quick end-to-end testing, `cloudflared` is the lowest-friction option. For ongoing development where you want to see each request the Add-on makes, `ngrok` is easier to debug.

Quick tunnel with `cloudflared`:

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```

Copy the printed `https://*.trycloudflare.com` URL into the `BACKEND_URL` Script Property.

Quick tunnel with `ngrok`:

```bash
brew install ngrok
ngrok config add-authtoken <your-authtoken>
ngrok http 8000
```

Copy the printed `https://*.ngrok-free.app` URL into the `BACKEND_URL` Script Property.

Both tunnel URLs change on every restart (unless you pay for a reserved domain), so you'll need to update `BACKEND_URL` each time.

Security: both tunnels expose your backend to the public internet. Use a strong `AUTH_BEARER_TOKEN` (`openssl rand -base64 32`) and stop the tunnel with `Ctrl+C` when you're done testing.

#### Viewing `appsscript.json` in the editor

Apps Script hides the manifest file by default. To edit it:

1. In the Apps Script editor, click the **gear icon** (⚙️ Project Settings) in the left sidebar.
2. Check **"Show 'appsscript.json' manifest file in editor"**.
3. Go back to the **Editor** (`<>` icon in the left sidebar).
4. `appsscript.json` now appears alongside `Code.gs` in the Files panel. Paste in the contents of [`addon/appsscript.json`](addon/appsscript.json).

#### Deploying the Add-on for testing

For day-to-day testing you use a **test deployment**, not a full Google Workspace Marketplace publication. A test deployment installs the Add-on only for your account (or Workspace domain, if signed in with a Workspace account) and can be updated as often as you like.

Before you deploy:

- Make sure `BACKEND_URL` and `BEARER_TOKEN` Script Properties are set.
- Make sure your tunnel is running and reachable at `BACKEND_URL`.
- Make sure the linked Google Cloud project has the **Gmail API** enabled and your account is listed as a test user on the OAuth consent screen (same project you use for the backend OAuth, or a separate project — either works).

Link a Google Cloud project (required for Gmail Add-ons):

1. In Apps Script, open **⚙️ Project Settings → Google Cloud Platform (GCP) Project**.
2. Click **Change project** and paste your project number (from Google Cloud Console → **IAM & Admin → Settings → Project number**).
3. Save.

Create a test deployment:

1. Click the blue **Deploy** button (top right) → **Test deployments**.
2. Click **Install**.
3. In the **Application(s)** section, make sure **Gmail** is selected.
4. Click **Done**.

Authorize the Add-on:

1. Open [Gmail](https://mail.google.com/) (refresh if it was already open).
2. Open any email.
3. Look for the Wayonagio Add-on icon in the right-hand sidebar (or under the **⋯** overflow menu). Click it.
4. You will be prompted to authorize the Add-on. Accept the scopes (`gmail.addons.execute`, `gmail.readonly`, `script.external_request`).
5. The Add-on card should now show **🇮🇹 Draft in Italian** and **🇵🇪 Draft in Spanish** buttons.

Click one of the buttons. You should see a "Draft created" notification and a new draft appear in the thread within a few seconds.

Updating the deployment:

- Any change you save in the Apps Script editor is picked up automatically by the test deployment the next time the Add-on runs. No redeploy needed.
- If you change `appsscript.json` scopes, reopen Gmail and re-authorize. See [Forcing re-authorization after a scope change](#forcing-re-authorization-after-a-scope-change) below if Gmail keeps showing the old permissions.

Troubleshooting:

- **Add-on icon doesn't appear**: refresh Gmail, or check **Settings → Add-ons** in Gmail and make sure the Add-on is installed and enabled.
- **"Authorization required" loop**: reinstall via **Deploy → Test deployments → Install** and re-authorize.
- **"You do not have permission to call UrlFetchApp.fetch" / "Specified permissions are not sufficient"**: the manifest is missing `https://www.googleapis.com/auth/script.external_request`, or Google is still using a cached authorization from before you added it. Confirm the scope is present in `appsscript.json`, then follow [Forcing re-authorization after a scope change](#forcing-re-authorization-after-a-scope-change).
- **"Backend error 401/403"**: the `BEARER_TOKEN` Script Property doesn't match `AUTH_BEARER_TOKEN` in `.env`. Update and retry (no redeploy needed).
- **"Backend error 404" or connection errors**: `BACKEND_URL` is stale — your tunnel URL changed. Update the Script Property.
- **Nothing happens after clicking a button**: open **Apps Script → Executions** (left sidebar) to see the Add-on's execution log, and check the backend logs in your terminal.

##### Forcing re-authorization after a scope change

Google caches the scopes you've granted per-user. When you add a new scope to `appsscript.json`, Gmail keeps using the old (smaller) grant until you explicitly revoke it, which produces errors like *"You do not have permission to call UrlFetchApp.fetch"* even though the manifest looks correct.

To force a fresh authorization:

1. **Confirm the updated manifest is saved.** In Apps Script, open `appsscript.json`, verify the new scope is present, and save (Cmd+S). The editor should no longer show "Unsaved changes". Cross-check in **⚙️ Project Settings → OAuth Scopes** — the new scope should be listed there.
2. **Uninstall the test deployment.** **Deploy → Test deployments → Uninstall** (if present).
3. **Revoke the Add-on in your Google Account.** Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions), find **"Wayonagio Draft Reply"** (or whatever the Add-on is named), click it, then **Remove access**. This is the step most often missed.
4. **Reinstall the test deployment.** **Deploy → Test deployments → Install**.
5. **Fully close and reopen Gmail** (not just refresh — close the tab).
6. **Click the Add-on icon** in Gmail's right sidebar. Google should now prompt you to grant the new permission (e.g. *"Connect to an external service"*). Accept.
7. **Retry the draft button.** It should now succeed.

You do **not** need to publish to the Google Workspace Marketplace for internal use. Publishing is only required if you want to install the Add-on outside your own domain.

#### Optional: sync the Add-on from this repo with `clasp`

Apps Script has no native GitHub integration, but Google's [`clasp`](https://github.com/google/clasp) CLI lets you push the files in this repo straight into your Apps Script project so you don't have to copy-paste after every edit.

Install and sign in:

```bash
npm install -g @google/clasp
clasp login
```

Link the `addon/` folder to your Apps Script project (get the Script ID from Apps Script → ⚙️ Project Settings → **IDs → Script ID**):

```bash
cd addon
clasp clone <your-script-id> --rootDir .
```

After editing `addon/Code.gs` or `addon/appsscript.json` locally, push to Apps Script:

```bash
clasp push
```

`clasp clone` creates a `.clasp.json` file in `addon/`; it's already covered by `.gitignore`.

### 8. Recommended checks

Verify these before moving beyond local testing:
- Drafts are created, never sent.
- Replies stay in the original Gmail thread.
- Language detection looks reasonable for Italian, Spanish, and English emails.
- Invalid bearer tokens are rejected.
- `SCANNER_ENABLED=false` prevents the automatic scanner from starting.

## Architecture

```
src/wayonagio_email_agent/
  gmail_client.py     # Gmail API: OAuth, list/get/draft/dedup
  llm/client.py       # LiteLLM-backed LLM: detect_language, generate_reply, is_travel_related
  agent.py            # Orchestration: manual flow + scanner loop
  api.py              # FastAPI: POST /draft-reply
  cli.py              # CLI: auth, list, draft-reply, scan
  state.py            # SQLite dedup state for scanner
addon/
  Code.gs             # Apps Script: Gmail contextual Add-on
  appsscript.json
Dockerfile            # Cloud Run-ready container image
tests/
  test_llm.py
  test_agent.py
  test_api.py
```

See `AGENTS.md` for developer guidance.
