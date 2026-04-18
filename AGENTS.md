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

# Run the scanner loop (requires SCANNER_ENABLED=true)
uv run python -m wayonagio_email_agent.cli scan --interval 1800

# Run a single scan pass (Cloud Run Job / Scheduler entrypoint)
uv run python -m wayonagio_email_agent.cli scan-once

# Rebuild the knowledge base from Drive (requires KB_ENABLED=true)
uv run python -m wayonagio_email_agent.cli kb-ingest
uv run python -m wayonagio_email_agent.cli kb-search "sample query"

# Build and run the container locally (see README for Cloud Run deploy)
docker build -t wayonagio-email-agent:dev .
docker run --rm -p 8080:8080 --env-file .env wayonagio-email-agent:dev
```

## Architecture

```
src/wayonagio_email_agent/
  gmail_client.py     # Gmail + Drive API wrapper (OAuth2: gmail.readonly, gmail.compose, drive.readonly)
  llm/client.py       # LiteLLM-backed LLM client (generate_reply, is_travel_related, detect_language)
  agent.py            # Orchestration: manual draft flow + automatic scan loop
  api.py              # FastAPI: POST /draft-reply (Bearer auth required)
  cli.py              # Admin CLI: list, draft-reply, scan, scan-once, kb-ingest, kb-search
  kb/                 # Optional knowledge base (KB_ENABLED)
    config.py         # Env-driven config + Drive URL/ID parsing
    drive.py          # Drive wrapper: list folders, export Docs, download files
    extract.py        # MIME-dispatched text extraction (PDF/Docs/txt/md)
    chunk.py          # Paragraph-aware chunker with overlap
    embed.py          # LiteLLM-backed batched embeddings
    store.py          # SQLite vector store + numpy cosine top-k
    artifact.py       # GCS + local artifact I/O
    retrieve.py       # Runtime API consumed by llm/client
    ingest.py         # End-to-end ingest pipeline
addon/                # Google Workspace Add-on (Apps Script)
  appsscript.json
  Code.gs             # Contextual trigger, language buttons → POST /draft-reply
Dockerfile            # Cloud Run-ready container image
```

**Data flow (manual trigger)**:
Gmail Add-on → `POST /draft-reply` (HTTPS + Bearer) → `agent.py` → `gmail_client.py` (fetch message) + `llm/client.py` (generate reply via LiteLLM, optionally augmented by `kb/retrieve.py`) → `gmail_client.py` (create draft)

**Data flow (automatic scanner)**:
Scanner loop → list unread → `is_travel_related()` (simple yes/no prompt) → if yes, same draft flow as above

**Data flow (KB ingest)**:
Cloud Scheduler → Cloud Run Job (`cli kb-ingest`) → `kb/ingest.py` → `kb/drive.py` (walk configured folders) → `kb/extract.py` → `kb/chunk.py` → `kb/embed.py` (LiteLLM embeddings) → `kb/store.py` → `kb/artifact.py` (publish `kb_index.sqlite` to GCS)

## Key Design Decisions

- **LLM**: provider-agnostic via [LiteLLM](https://docs.litellm.ai/). `LLM_MODEL` env var (format `<provider>/<model>`) chooses the backend. Supported in production: `gemini/gemini-2.5-flash` (recommended) and `ollama/<model>` (self-hosted). Legacy `OLLAMA_MODEL` is still honored for back-compat. Adding another provider is a config change, not code.
- **Gmail/Drive scopes**: `gmail.readonly` + `gmail.compose` + `drive.readonly` only. No `gmail.send` scope; `drive.readonly` so the ingest Job can read Drive content for the optional KB.
- **Authentication**: All API endpoints require `Authorization: Bearer <AUTH_BEARER_TOKEN>`. The server must run behind HTTPS/TLS (Cloud Run provides this automatically; self-hosted uses Caddy/Nginx) — never expose plain HTTP.
- **Recommended deployment**: Cloud Run + Gemini. Secrets (Gmail OAuth, bearer token, Gemini API key) live in Secret Manager. See README.
- **Classification**: `is_travel_related()` is intentionally simple — one short prompt, yes/no + language code. Do not over-engineer it.
- **Draft MIME**: Replies must include correct `In-Reply-To`, `References`, `Re:` prefix, and `threadId`.
- **Knowledge base**: opt-in (`KB_ENABLED=false` by default). Contents of the Drive folders listed in `KB_RAG_FOLDER_IDS` are chunked, embedded, and retrieved per email so replies can cite agency-specific facts. Vector store is SQLite + numpy cosine (trivially replaceable later); embeddings default to `gemini/text-embedding-004`. KB failures are always non-fatal — the agent falls back to the base prompt rather than refusing to draft.

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
| `SCANNER_ENABLED` | Feature flag for the automatic scanner (default `false`) |
| `KB_ENABLED` | Feature flag for the knowledge base (default `false`) |
| `KB_RAG_FOLDER_IDS` | Comma-separated Drive folder IDs or share URLs for RAG content (required when KB is on) |
| `KB_EMBEDDING_MODEL` | LiteLLM embedding model (default `gemini/text-embedding-004`) |
| `KB_GCS_URI` | `gs://bucket[/prefix]` where ingest writes and runtime reads artifacts |
| `KB_LOCAL_DIR` | Dev fallback for artifacts when `KB_GCS_URI` is unset (default `./kb_artifacts`) |
| `KB_TOP_K` | Chunks to retrieve per email (default `4`) |

`credentials.json` and `token.json` must be listed in `.gitignore`.

## Gmail Add-on (addon/)

Apps Script project. Deployed as a Google Workspace Add-on with a contextual trigger on Gmail. The `Code.gs` file reads the current `message_id` from the Gmail context, stores the backend URL and Bearer token in Apps Script script properties, and calls `POST /draft-reply`. Document installation steps for the Workspace domain in the README.
