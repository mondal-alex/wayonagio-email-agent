# Wayonagio Email Agent

A **draft-only** email response agent for a Cusco (Peru) travel agency. It connects to Gmail via the Gmail API and uses a self-hosted [Ollama](https://ollama.com) LLM to generate multilingual draft replies (Italian, Spanish, English). Staff trigger drafts via a Gmail Add-on button; an automatic scanner creates drafts for new travel-related emails.

**The agent never sends email.** It only calls `drafts.create`. Sending is always done manually by staff in Gmail.

## Prerequisites

- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com) running locally or on the server (`ollama serve`)
- A Google Cloud project with the Gmail API enabled and OAuth 2.0 credentials downloaded

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

| Variable | Description |
|---|---|
| `GMAIL_CREDENTIALS_PATH` | Path to `credentials.json` (OAuth client secrets) |
| `GMAIL_TOKEN_PATH` | Path for persisted OAuth token (written by `cli auth`) |
| `OLLAMA_BASE_URL` | Ollama server URL (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | Model name, e.g. `llama3.2` or `mistral` |
| `AUTH_BEARER_TOKEN` | Bearer token for API authentication |
| `SCANNER_ENABLED` | Feature flag for automatic scanner (`false` by default) |
| `SCANNER_STATE_DB` | SQLite DB path for scanner dedup state (default: `scanner_state.db`) |
| `LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

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

## Server deployment

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
6. Copy in the contents of [`addon/Code.gs`](addon/Code.gs) and [`addon/appsscript.json`](addon/appsscript.json).
7. In **Project Settings → Script Properties**, set:
   - `BACKEND_URL=<your public tunnel URL>`
   - `BEARER_TOKEN=<AUTH_BEARER_TOKEN from .env>`
8. Deploy it as a Google Workspace Add-on.
9. Install it for your test account.
10. Open a Gmail message and click **Draft reply**.

You should see a notification in Gmail and a new draft should appear in the thread.

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
  llm/ollama.py       # Ollama: detect_language, generate_reply, is_travel_related
  agent.py            # Orchestration: manual flow + scanner loop
  api.py              # FastAPI: POST /draft-reply
  cli.py              # CLI: auth, list, draft-reply, scan
  state.py            # SQLite dedup state for scanner
addon/
  Code.gs             # Apps Script: Gmail contextual Add-on
  appsscript.json
tests/
  test_llm.py
  test_agent.py
  test_api.py
```

See `AGENTS.md` for developer guidance.
