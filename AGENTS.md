# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

A Python email response agent for a Cusco (Peru) travel agency. It connects to Gmail via the Gmail API and uses an LLM (Google Gemini in production via Cloud Run, or self-hosted Ollama for local/offline) to generate **draft-only** multilingual replies (Italian, Spanish, English). LLM calls go through LiteLLM so the provider is a config choice. Staff interact via a Gmail Add-on (Apps Script); there is no separate web UI. An automatic scanner creates drafts for new travel-related emails.

**Critical constraint**: The app must never call `drafts.send` or `messages.send` — only `drafts.create`.

## Commands

This project uses `uv` as the package manager (Python 3.13).

```bash
# Install dependencies
uv sync

# Add a new dependency
uv add <package>

# Run the FastAPI server
uv run uvicorn wayonagio_email_agent.api:app --host 0.0.0.0

# Run the CLI
uv run python -m wayonagio_email_agent.cli list
uv run python -m wayonagio_email_agent.cli draft-reply <message_id>

# Run the scanner (requires SCANNER_ENABLED=true)
uv run python -m wayonagio_email_agent.cli scan --interval 1800

# Build and run the container locally (see README for Cloud Run deploy)
docker build -t wayonagio-email-agent:dev .
docker run --rm -p 8080:8080 --env-file .env wayonagio-email-agent:dev
```

## Architecture

```
src/wayonagio_email_agent/
  gmail_client.py     # Gmail API wrapper (OAuth2, list/get/draft)
  llm/client.py       # LiteLLM-backed LLM client (generate_reply, is_travel_related, detect_language)
  agent.py            # Orchestration: manual draft flow + automatic scan loop
  api.py              # FastAPI: POST /draft-reply (Bearer auth required)
  cli.py              # Admin CLI: list, draft-reply, scan subcommands
addon/                # Google Workspace Add-on (Apps Script)
  appsscript.json
  Code.gs             # Contextual trigger, "Draft reply" button → POST /draft-reply
Dockerfile            # Cloud Run-ready container image
```

**Data flow (manual trigger)**:
Gmail Add-on → `POST /draft-reply` (HTTPS + Bearer) → `agent.py` → `gmail_client.py` (fetch message) + `llm/client.py` (generate reply via LiteLLM) → `gmail_client.py` (create draft)

**Data flow (automatic scanner)**:
Scanner loop → list unread → `is_travel_related()` (simple yes/no prompt) → if yes, same draft flow as above

## Key Design Decisions

- **LLM**: provider-agnostic via [LiteLLM](https://docs.litellm.ai/). `LLM_MODEL` env var (format `<provider>/<model>`) chooses the backend. Supported in production: `gemini/gemini-2.5-flash` (recommended) and `ollama/<model>` (self-hosted). Legacy `OLLAMA_MODEL` is still honored for back-compat. Adding another provider is a config change, not code.
- **Gmail scopes**: `gmail.readonly` + `gmail.compose` only. No `gmail.send` scope.
- **Authentication**: All API endpoints require `Authorization: Bearer <AUTH_BEARER_TOKEN>`. The server must run behind HTTPS/TLS (Cloud Run provides this automatically; self-hosted uses Caddy/Nginx) — never expose plain HTTP.
- **Recommended deployment**: Cloud Run + Gemini. Secrets (Gmail OAuth, bearer token, Gemini API key) live in Secret Manager. See README.
- **Classification**: `is_travel_related()` is intentionally simple — one short prompt, yes/no + language code. Do not over-engineer it.
- **Draft MIME**: Replies must include correct `In-Reply-To`, `References`, `Re:` prefix, and `threadId`.

## Environment Variables

Defined in `.env` (never committed):

| Variable | Description |
|---|---|
| `GMAIL_CREDENTIALS_PATH` | Path to OAuth client secrets JSON (e.g. `credentials.json`) |
| `GMAIL_TOKEN_PATH` | Path to persisted OAuth token (e.g. `token.json`) |
| `LLM_MODEL` | LiteLLM model string: `gemini/gemini-2.5-flash`, `ollama/llama3.2`, etc. |
| `GEMINI_API_KEY` | Google AI Studio API key (required when `LLM_MODEL` starts with `gemini/`) |
| `OLLAMA_BASE_URL` | Ollama server URL (default: `http://localhost:11434`) — Ollama only |
| `OLLAMA_MODEL` | Legacy back-compat: if `LLM_MODEL` is unset, used as `ollama/<OLLAMA_MODEL>` |
| `OLLAMA_KEEP_ALIVE` | How long Ollama keeps the model loaded (default `1h`) — Ollama only |
| `AUTH_BEARER_TOKEN` | Bearer token for API authentication |

`credentials.json` and `token.json` must be listed in `.gitignore`.

## Gmail Add-on (addon/)

Apps Script project. Deployed as a Google Workspace Add-on with a contextual trigger on Gmail. The `Code.gs` file reads the current `message_id` from the Gmail context, stores the backend URL and Bearer token in Apps Script script properties, and calls `POST /draft-reply`. Document installation steps for the Workspace domain in the README.
