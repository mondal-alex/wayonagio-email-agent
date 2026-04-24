#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/cloud/common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<'EOF'
Deploy Wayonagio Email Agent from scratch to Cloud Run.

Required env vars:
  PROJECT_ID                  GCP project ID (string, not numeric)
  RAG_FOLDER_IDS              Comma-separated Drive folder IDs or URLs
  GEMINI_API_KEY              Gemini API key value

Optional env vars:
  EXEMPLAR_FOLDER_IDS         Optional comma-separated exemplar Drive folders
  PROJECT_NUMBER              Auto-detected if omitted
  REGION                      Default: us-central1
  REPOSITORY                  Default: wayonagio
  SERVICE_NAME                Default: wayonagio-email-agent
  JOB_NAME                    Default: wayonagio-kb-ingest
  SERVICE_ACCOUNT_NAME        Default: wayonagio-run
  KB_BUCKET_NAME              Default: wayonagio-kb
  IMAGE_TAG                   Default: latest
  AUTH_BEARER_TOKEN           Generated randomly if omitted
  CREDENTIALS_FILE            Default: ./credentials.json
  TOKEN_FILE                  Default: ./token.json
  SCANNER_ENABLED             Default: false
  LOG_LEVEL                   Default: INFO
  LLM_MODEL                   Default: gemini/gemini-2.5-flash
  KB_EMBEDDING_MODEL          Default: gemini/gemini-embedding-001
  KB_TOP_K                    Default: 4
  SKIP_LOCAL_IMAGE_TEST       Set to true to skip local docker smoke test
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_cmd gcloud
require_cmd docker
require_cmd openssl

set_defaults
require_env PROJECT_ID
require_env RAG_FOLDER_IDS
require_env GEMINI_API_KEY

: "${CREDENTIALS_FILE:=${REPO_ROOT}/credentials.json}"
: "${TOKEN_FILE:=${REPO_ROOT}/token.json}"

[[ -f "${CREDENTIALS_FILE}" ]] || die "Missing credentials file: ${CREDENTIALS_FILE}"
[[ -f "${TOKEN_FILE}" ]] || die "Missing token file: ${TOKEN_FILE}"

if [[ -z "${PROJECT_NUMBER:-}" ]]; then
  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
fi
require_env PROJECT_NUMBER

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
IMAGE_URI="us-central1-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/email-agent:${IMAGE_TAG}"
KB_GCS_URI="gs://${KB_BUCKET_NAME}"
CLOUDBUILD_BUCKET="gs://${PROJECT_ID}_cloudbuild"
SERVICE_ENV_VARS="^@^LLM_MODEL=${LLM_MODEL}@GMAIL_CREDENTIALS_PATH=/secrets/gmail-credentials/credentials.json@GMAIL_TOKEN_PATH=/secrets/gmail-token/token.json@SCANNER_ENABLED=${SCANNER_ENABLED}@LOG_LEVEL=${LOG_LEVEL}@KB_GCS_URI=${KB_GCS_URI}@KB_EMBEDDING_MODEL=${KB_EMBEDDING_MODEL}@KB_TOP_K=${KB_TOP_K}@KB_RAG_FOLDER_IDS=${RAG_FOLDER_IDS}@KB_EXEMPLAR_FOLDER_IDS=${EXEMPLAR_FOLDER_IDS:-}"
JOB_ENV_VARS="^@^LLM_MODEL=${LLM_MODEL}@KB_GCS_URI=${KB_GCS_URI}@KB_EMBEDDING_MODEL=${KB_EMBEDDING_MODEL}@KB_RAG_FOLDER_IDS=${RAG_FOLDER_IDS}@GMAIL_CREDENTIALS_PATH=/secrets/gmail-credentials/credentials.json@GMAIL_TOKEN_PATH=/secrets/gmail-token/token.json"

info "Using project: ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

info "Enabling required APIs"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  --project="${PROJECT_ID}"

if gcloud artifacts repositories describe "${REPOSITORY}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  info "Artifact Registry repo exists: ${REPOSITORY}"
else
  info "Creating Artifact Registry repo: ${REPOSITORY}"
  gcloud artifacts repositories create "${REPOSITORY}" \
    --project="${PROJECT_ID}" \
    --repository-format=docker \
    --location="${REGION}"
fi

if gcloud storage buckets describe "${KB_GCS_URI}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  info "KB bucket exists: ${KB_GCS_URI}"
else
  info "Creating KB bucket: ${KB_GCS_URI}"
  gcloud storage buckets create "${KB_GCS_URI}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
fi

if [[ "${SKIP_LOCAL_IMAGE_TEST:-false}" != "true" ]]; then
  info "Running optional local Docker smoke test"
  docker build -t wayonagio-email-agent:dev "${REPO_ROOT}" >/dev/null
  docker run --rm -d --name wayonagio-email-agent-smoke \
    -p 18080:8080 \
    --env-file "${REPO_ROOT}/.env" \
    -v "${CREDENTIALS_FILE}:/app/credentials.json:ro" \
    -v "${TOKEN_FILE}:/app/token.json:ro" \
    wayonagio-email-agent:dev >/dev/null
  sleep 3
  if ! curl -fsS "http://localhost:18080/healthz" >/dev/null 2>&1; then
    warn "Local health check failed (continuing)."
  fi
  docker rm -f wayonagio-email-agent-smoke >/dev/null 2>&1 || true
fi

info "Upserting secrets"
ensure_secret_version "${GMAIL_CREDENTIALS_SECRET_NAME}" "${CREDENTIALS_FILE}"
ensure_secret_version "${GMAIL_TOKEN_SECRET_NAME}" "${TOKEN_FILE}"
ensure_secret_version_from_stdin "${GEMINI_API_KEY_SECRET_NAME}" "${GEMINI_API_KEY}"

if [[ -z "${AUTH_BEARER_TOKEN:-}" ]]; then
  AUTH_BEARER_TOKEN="$(openssl rand -base64 32)"
  info "Generated AUTH_BEARER_TOKEN (save this for Apps Script BEARER_TOKEN):"
  printf '%s\n' "${AUTH_BEARER_TOKEN}"
fi
ensure_secret_version_from_stdin "${AUTH_BEARER_SECRET_NAME}" "${AUTH_BEARER_TOKEN}"

if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  info "Service account exists: ${SA_EMAIL}"
else
  info "Creating service account: ${SA_EMAIL}"
  gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
    --project="${PROJECT_ID}" \
    --display-name="Wayonagio Email Agent (Cloud Run)"
fi

for secret in \
  "${GMAIL_CREDENTIALS_SECRET_NAME}" \
  "${GMAIL_TOKEN_SECRET_NAME}" \
  "${AUTH_BEARER_SECRET_NAME}" \
  "${GEMINI_API_KEY_SECRET_NAME}"; do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
done
info "Granted Secret Manager access to ${SA_EMAIL}"

gcloud storage buckets add-iam-policy-binding "${KB_GCS_URI}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" >/dev/null
info "Granted bucket access to ${SA_EMAIL}"

gcloud storage buckets add-iam-policy-binding "${CLOUDBUILD_BUCKET}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/storage.objectViewer" >/dev/null || true
gcloud artifacts repositories add-iam-policy-binding "${REPOSITORY}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/artifactregistry.writer" >/dev/null || true
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/logging.logWriter" >/dev/null || true
info "Applied Cloud Build IAM permissions"

info "Building and pushing image: ${IMAGE_URI}"
gcloud builds submit "${REPO_ROOT}" \
  --project="${PROJECT_ID}" \
  --tag "${IMAGE_URI}"

info "Deploying Cloud Run service: ${SERVICE_NAME}"
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
  --set-secrets="AUTH_BEARER_TOKEN=${AUTH_BEARER_SECRET_NAME}:latest,GEMINI_API_KEY=${GEMINI_API_KEY_SECRET_NAME}:latest" \
  --set-secrets="/secrets/gmail-credentials/credentials.json=${GMAIL_CREDENTIALS_SECRET_NAME}:latest,/secrets/gmail-token/token.json=${GMAIL_TOKEN_SECRET_NAME}:latest"

if gcloud run jobs describe "${JOB_NAME}" --project="${PROJECT_ID}" --region="${REGION}" >/dev/null 2>&1; then
  info "Updating ingest job: ${JOB_NAME}"
  gcloud run jobs update "${JOB_NAME}" \
    --project="${PROJECT_ID}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --service-account="${SA_EMAIL}" \
    --command="python" \
    --args="-m,wayonagio_email_agent.cli,kb-ingest" \
    --set-env-vars="${JOB_ENV_VARS}" \
    --set-secrets="AUTH_BEARER_TOKEN=${AUTH_BEARER_SECRET_NAME}:latest,GEMINI_API_KEY=${GEMINI_API_KEY_SECRET_NAME}:latest" \
    --set-secrets="/secrets/gmail-credentials/credentials.json=${GMAIL_CREDENTIALS_SECRET_NAME}:latest,/secrets/gmail-token/token.json=${GMAIL_TOKEN_SECRET_NAME}:latest"
else
  info "Creating ingest job: ${JOB_NAME}"
  gcloud run jobs create "${JOB_NAME}" \
    --project="${PROJECT_ID}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --service-account="${SA_EMAIL}" \
    --command="python" \
    --args="-m,wayonagio_email_agent.cli,kb-ingest" \
    --set-env-vars="${JOB_ENV_VARS}" \
    --set-secrets="AUTH_BEARER_TOKEN=${AUTH_BEARER_SECRET_NAME}:latest,GEMINI_API_KEY=${GEMINI_API_KEY_SECRET_NAME}:latest" \
    --set-secrets="/secrets/gmail-credentials/credentials.json=${GMAIL_CREDENTIALS_SECRET_NAME}:latest,/secrets/gmail-token/token.json=${GMAIL_TOKEN_SECRET_NAME}:latest"
fi

info "Running one-time KB ingest job"
gcloud run jobs execute "${JOB_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --wait

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
printf '\nDeployment complete.\n'
printf 'Cloud Run URL: %s\n' "${SERVICE_URL}"
printf 'Service Account: %s\n' "${SA_EMAIL}"
printf 'Image: %s\n' "${IMAGE_URI}"
printf 'Set Apps Script BACKEND_URL=%s and BEARER_TOKEN=<token above>\n' "${SERVICE_URL}"
