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
5. Go to **OAuth consent screen** and add the Gmail account as a test user (while in testing mode).

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
| `SCANNER_STATE_DB` | SQLite DB path for scanner dedup state (default: `scanner_state.db`) |
| `LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

## First-time authentication

Run this **once on the server** to open a browser, complete the OAuth flow, and write `token.json`:

```bash
uv run python -m wayonagio_email_agent.cli auth
```

The token is refreshed automatically on subsequent runs. If the refresh token is ever revoked, re-run `cli auth`.

## Running

### API server (used by the Gmail Add-on)

```bash
uv run uvicorn wayonagio_email_agent.api:app --host 0.0.0.0
```

Must be run behind a reverse proxy (Caddy or Nginx) that terminates HTTPS. See [Server deployment](#server-deployment).

### Automatic scanner

```bash
# Run continuously, scanning every 30 minutes
uv run python -m wayonagio_email_agent.cli scan --interval 1800

# Test classification without creating any drafts
uv run python -m wayonagio_email_agent.cli scan --dry-run
```

### CLI (admin)

```bash
# List recent unread emails
uv run python -m wayonagio_email_agent.cli list

# Create a draft for a specific message
uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
```

## Gmail Add-on setup

The Add-on lives in `addon/`. It adds a **"Draft reply"** button inside Gmail when viewing an email.

1. Go to [script.google.com](https://script.google.com) and create a new project.
2. Copy the contents of `addon/Code.gs` and `addon/appsscript.json` into the project.
3. In the Apps Script editor, go to **Project Settings → Script Properties** and add:
   - `BACKEND_URL` — your server URL, e.g. `https://your-server.example.com`
   - `BEARER_TOKEN` — the same value as `AUTH_BEARER_TOKEN` in your `.env`
4. Click **Deploy → New Deployment** → type **Google Workspace Add-on**.
5. Install the Add-on for your Workspace domain via the Admin console, or install it for yourself via the deployment URL.

When a staff member opens an email in Gmail, the Add-on panel appears on the right. Clicking **"Draft reply"** calls `POST /draft-reply` on the backend and a draft appears in the Gmail thread.

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

## Tests

```bash
uv run pytest
```

All tests use mocked Gmail and Ollama calls — no real credentials or running services needed.

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
