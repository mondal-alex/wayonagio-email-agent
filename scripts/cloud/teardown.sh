#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/cloud/common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<'EOF'
Tear down Wayonagio Email Agent resources in GCP.

Required env vars:
  PROJECT_ID

Optional env vars:
  REGION                      Default: us-central1
  REPOSITORY                  Default: wayonagio
  SERVICE_NAME                Default: wayonagio-email-agent
  JOB_NAME                    Default: wayonagio-kb-ingest
  SCHEDULER_JOB_NAME          Default: wayonagio-kb-ingest-yearly
  SERVICE_ACCOUNT_NAME        Default: wayonagio-run
  KB_BUCKET_NAME              Default: wayonagio-kb
  DELETE_SECRETS              true|false (default false)
  DELETE_BUCKET               true|false (default false)
  DELETE_REPOSITORY           true|false (default false)
  DELETE_SERVICE_ACCOUNT      true|false (default false)
  FORCE                       true|false (default false)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_cmd gcloud
set_defaults
require_env PROJECT_ID

: "${DELETE_SECRETS:=false}"
: "${DELETE_BUCKET:=false}"
: "${DELETE_REPOSITORY:=false}"
: "${DELETE_SERVICE_ACCOUNT:=false}"
: "${FORCE:=false}"

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
KB_GCS_URI="gs://${KB_BUCKET_NAME}"

if [[ "${FORCE}" != "true" ]]; then
  printf 'This will delete Cloud Run resources for project "%s". Type "yes" to continue: ' "${PROJECT_ID}"
  read -r answer
  [[ "${answer}" == "yes" ]] || die "Aborted."
fi

info "Deleting scheduler job (if present): ${SCHEDULER_JOB_NAME}"
gcloud scheduler jobs delete "${SCHEDULER_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --quiet >/dev/null 2>&1 || true

info "Deleting ingest job (if present): ${JOB_NAME}"
gcloud run jobs delete "${JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --quiet >/dev/null 2>&1 || true

info "Deleting Cloud Run service (if present): ${SERVICE_NAME}"
gcloud run services delete "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --quiet >/dev/null 2>&1 || true

if [[ "${DELETE_REPOSITORY}" == "true" ]]; then
  info "Deleting Artifact Registry repository: ${REPOSITORY}"
  gcloud artifacts repositories delete "${REPOSITORY}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --quiet >/dev/null 2>&1 || true
fi

if [[ "${DELETE_BUCKET}" == "true" ]]; then
  info "Deleting KB bucket and all objects: ${KB_GCS_URI}"
  gcloud storage rm --recursive "${KB_GCS_URI}" >/dev/null 2>&1 || true
  gcloud storage buckets delete "${KB_GCS_URI}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
fi

if [[ "${DELETE_SECRETS}" == "true" ]]; then
  for secret in auth-bearer-token gemini-api-key gmail-credentials gmail-token; do
    info "Deleting secret (if present): ${secret}"
    gcloud secrets delete "${secret}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
  done
fi

if [[ "${DELETE_SERVICE_ACCOUNT}" == "true" ]]; then
  info "Deleting service account (if present): ${SA_EMAIL}"
  gcloud iam service-accounts delete "${SA_EMAIL}" \
    --project="${PROJECT_ID}" \
    --quiet >/dev/null 2>&1 || true
fi

printf '\nTeardown complete for project %s.\n' "${PROJECT_ID}"
