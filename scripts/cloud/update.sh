#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/cloud/common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<'EOF'
Update an existing Wayonagio Cloud Run deployment (new code/dependencies).

Required env vars:
  PROJECT_ID
  RAG_FOLDER_IDS

Optional env vars:
  REGION                      Default: us-central1
  REPOSITORY                  Default: wayonagio
  SERVICE_NAME                Default: wayonagio-email-agent
  JOB_NAME                    Default: wayonagio-kb-ingest
  SERVICE_ACCOUNT_NAME        Default: wayonagio-run
  IMAGE_TAG                   Default: latest
  SCANNER_ENABLED             Default: false
  LOG_LEVEL                   Default: INFO
  LLM_MODEL                   Default: gemini/gemini-2.5-flash
  KB_EMBEDDING_MODEL          Default: gemini/gemini-embedding-001
  KB_TOP_K                    Default: 4
  KB_BUCKET_NAME              Default: wayonagio-kb
  EXEMPLAR_FOLDER_IDS         Optional comma-separated exemplar Drive folders
  RUN_KB_INGEST               true|false (default false)
  CREDENTIALS_FILE            If set, uploads a new gmail-credentials secret version
  TOKEN_FILE                  If set, uploads a new gmail-token secret version
  GEMINI_API_KEY              If set, uploads a new gemini-api-key secret version
  AUTH_BEARER_TOKEN           If set, uploads a new auth-bearer-token secret version
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_cmd gcloud
set_defaults
require_env PROJECT_ID
require_env RAG_FOLDER_IDS

: "${RUN_KB_INGEST:=false}"

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_URI="us-central1-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/email-agent:${IMAGE_TAG}"
KB_GCS_URI="gs://${KB_BUCKET_NAME}"
SERVICE_ENV_VARS="^@^LLM_MODEL=${LLM_MODEL}@GMAIL_CREDENTIALS_PATH=/secrets/gmail-credentials/credentials.json@GMAIL_TOKEN_PATH=/secrets/gmail-token/token.json@SCANNER_ENABLED=${SCANNER_ENABLED}@LOG_LEVEL=${LOG_LEVEL}@KB_GCS_URI=${KB_GCS_URI}@KB_EMBEDDING_MODEL=${KB_EMBEDDING_MODEL}@KB_TOP_K=${KB_TOP_K}@KB_RAG_FOLDER_IDS=${RAG_FOLDER_IDS}@KB_EXEMPLAR_FOLDER_IDS=${EXEMPLAR_FOLDER_IDS:-}"
JOB_ENV_VARS="^@^LLM_MODEL=${LLM_MODEL}@KB_GCS_URI=${KB_GCS_URI}@KB_EMBEDDING_MODEL=${KB_EMBEDDING_MODEL}@KB_RAG_FOLDER_IDS=${RAG_FOLDER_IDS}@GMAIL_CREDENTIALS_PATH=/secrets/gmail-credentials/credentials.json@GMAIL_TOKEN_PATH=/secrets/gmail-token/token.json"

if [[ -n "${CREDENTIALS_FILE:-}" ]]; then
  [[ -f "${CREDENTIALS_FILE}" ]] || die "Missing credentials file: ${CREDENTIALS_FILE}"
  ensure_secret_version "gmail-credentials" "${CREDENTIALS_FILE}"
fi
if [[ -n "${TOKEN_FILE:-}" ]]; then
  [[ -f "${TOKEN_FILE}" ]] || die "Missing token file: ${TOKEN_FILE}"
  ensure_secret_version "gmail-token" "${TOKEN_FILE}"
fi
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  ensure_secret_version_from_stdin "gemini-api-key" "${GEMINI_API_KEY}"
fi
if [[ -n "${AUTH_BEARER_TOKEN:-}" ]]; then
  ensure_secret_version_from_stdin "auth-bearer-token" "${AUTH_BEARER_TOKEN}"
fi

info "Building and pushing updated image: ${IMAGE_URI}"
gcloud builds submit "${REPO_ROOT}" \
  --project="${PROJECT_ID}" \
  --tag "${IMAGE_URI}"

info "Updating Cloud Run service: ${SERVICE_NAME}"
gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --image="${IMAGE_URI}" \
  --region="${REGION}" \
  --platform=managed \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --cpu=1 --memory=512Mi \
  --set-env-vars="${SERVICE_ENV_VARS}" \
  --set-secrets="AUTH_BEARER_TOKEN=auth-bearer-token:latest,GEMINI_API_KEY=gemini-api-key:latest" \
  --set-secrets="/secrets/gmail-credentials/credentials.json=gmail-credentials:latest,/secrets/gmail-token/token.json=gmail-token:latest"

if gcloud run jobs describe "${JOB_NAME}" --project="${PROJECT_ID}" --region="${REGION}" >/dev/null 2>&1; then
  info "Updating KB ingest job image: ${JOB_NAME}"
  gcloud run jobs update "${JOB_NAME}" \
    --project="${PROJECT_ID}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --service-account="${SA_EMAIL}" \
    --command="python" \
    --args="-m,wayonagio_email_agent.cli,kb-ingest" \
    --set-env-vars="${JOB_ENV_VARS}" \
    --set-secrets="AUTH_BEARER_TOKEN=auth-bearer-token:latest,GEMINI_API_KEY=gemini-api-key:latest" \
    --set-secrets="/secrets/gmail-credentials/credentials.json=gmail-credentials:latest,/secrets/gmail-token/token.json=gmail-token:latest"
fi

if [[ "${RUN_KB_INGEST}" == "true" ]]; then
  info "Executing KB ingest job"
  gcloud run jobs execute "${JOB_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --wait
fi

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
printf '\nUpdate complete.\n'
printf 'Cloud Run URL: %s\n' "${SERVICE_URL}"
printf 'Image: %s\n' "${IMAGE_URI}"
