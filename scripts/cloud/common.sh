#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

info() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "Missing required environment variable: ${name}"
}

resource_exists() {
  local kind="$1"
  shift
  if gcloud "$kind" describe "$@" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

ensure_secret_version() {
  local secret_name="$1"
  local source_file="$2"
  if gcloud secrets describe "$secret_name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets versions add "$secret_name" --project="$PROJECT_ID" --data-file="$source_file" >/dev/null
    info "Added new version for secret: ${secret_name}"
  else
    gcloud secrets create "$secret_name" --project="$PROJECT_ID" --data-file="$source_file" >/dev/null
    info "Created secret: ${secret_name}"
  fi
}

ensure_secret_version_from_stdin() {
  local secret_name="$1"
  local payload="$2"
  if gcloud secrets describe "$secret_name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$payload" | gcloud secrets versions add "$secret_name" --project="$PROJECT_ID" --data-file=- >/dev/null
    info "Added new version for secret: ${secret_name}"
  else
    printf '%s' "$payload" | gcloud secrets create "$secret_name" --project="$PROJECT_ID" --data-file=- >/dev/null
    info "Created secret: ${secret_name}"
  fi
}

set_defaults() {
  : "${REGION:=us-central1}"
  : "${REPOSITORY:=wayonagio}"
  : "${SERVICE_NAME:=wayonagio-email-agent}"
  : "${JOB_NAME:=wayonagio-kb-ingest}"
  : "${SCHEDULER_JOB_NAME:=wayonagio-kb-ingest-yearly}"
  : "${SERVICE_ACCOUNT_NAME:=wayonagio-run}"
  : "${KB_BUCKET_NAME:=wayonagio-kb}"
  : "${IMAGE_TAG:=latest}"
  : "${SCANNER_ENABLED:=false}"
  : "${LOG_LEVEL:=INFO}"
  : "${LLM_MODEL:=gemini/gemini-2.5-flash}"
  : "${KB_EMBEDDING_MODEL:=gemini/gemini-embedding-001}"
  : "${KB_TOP_K:=4}"
  : "${SCHEDULER_CRON:=15 4 15 1 *}"
  : "${AUTH_BEARER_SECRET_NAME:=auth-bearer-token}"
  : "${GEMINI_API_KEY_SECRET_NAME:=gemini-api-key}"
  : "${GMAIL_CREDENTIALS_SECRET_NAME:=gmail-credentials}"
  : "${GMAIL_TOKEN_SECRET_NAME:=gmail-token}"
}
