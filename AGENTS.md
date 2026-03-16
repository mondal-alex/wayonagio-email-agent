# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

A Python email response agent for a Cusco (Peru) travel agency. It connects to Gmail via the Gmail API and uses a self-hosted Ollama LLM to generate **draft-only** multilingual replies (Italian, Spanish, English). Staff interact via a Gmail Add-on (Apps Script); there is no separate web UI. An automatic scanner creates drafts for new travel-related emails.

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

# Run the scanner
uv run python -m wayonagio_email_agent scan --interval 30

# Run main entry point
uv run python main.py
```

## Architecture

```
src/wayonagio_email_agent/
  gmail_client.py     # Gmail API wrapper (OAuth2, list/get/draft)
  llm/ollama.py       # Ollama client (generate_reply, is_travel_related)
  agent.py            # Orchestration: manual draft flow + automatic scan loop
  api.py              # FastAPI: POST /draft-reply (Bearer auth required)
  cli.py              # Admin CLI: list, draft-reply, scan subcommands
addon/                # Google Workspace Add-on (Apps Script)
  appsscript.json
  Code.gs             # Contextual trigger, "Draft reply" button → POST /draft-reply
```

**Data flow (manual trigger)**:
Gmail Add-on → `POST /draft-reply` (HTTPS + Bearer) → `agent.py` → `gmail_client.py` (fetch message) + `llm/ollama.py` (generate reply) → `gmail_client.py` (create draft)

**Data flow (automatic scanner)**:
Scanner loop → list unread → `is_travel_related()` (simple yes/no prompt) → if yes, same draft flow as above

## Key Design Decisions

- **LLM**: Ollama only (`ollama` Python package). Use `Client(host=OLLAMA_BASE_URL)` with `OLLAMA_MODEL` from env. No cloud LLM.
- **Gmail scopes**: `gmail.readonly` + `gmail.compose` only. No `gmail.send` scope.
- **Authentication**: All API endpoints require `Authorization: Bearer <AUTH_BEARER_TOKEN>`. The server must run behind a reverse proxy (Caddy/Nginx) for HTTPS/TLS — never expose plain HTTP.
- **Classification**: `is_travel_related()` is intentionally simple — one short prompt, yes/no + language code. Do not over-engineer it.
- **Draft MIME**: Replies must include correct `In-Reply-To`, `References`, `Re:` prefix, and `threadId`.

## Environment Variables

Defined in `.env` (never committed):

| Variable | Description |
|---|---|
| `GMAIL_CREDENTIALS_PATH` | Path to OAuth client secrets JSON (e.g. `credentials.json`) |
| `GMAIL_TOKEN_PATH` | Path to persisted OAuth token (e.g. `token.json`) |
| `OLLAMA_BASE_URL` | Ollama server URL (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | Model name (e.g. `llama3.2`, `mistral`) |
| `AUTH_BEARER_TOKEN` | Bearer token for API authentication |

`credentials.json` and `token.json` must be listed in `.gitignore`.

## Gmail Add-on (addon/)

Apps Script project. Deployed as a Google Workspace Add-on with a contextual trigger on Gmail. The `Code.gs` file reads the current `message_id` from the Gmail context, stores the backend URL and Bearer token in Apps Script script properties, and calls `POST /draft-reply`. Document installation steps for the Workspace domain in the README.
