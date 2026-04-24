#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/cloud/common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<'EOF'
Check Cloud Run + Gmail OAuth token health for Wayonagio.

Required env vars:
  PROJECT_ID

Optional env vars:
  REGION        Default: us-central1
  SERVICE_NAME  Default: wayonagio-email-agent
  LIMIT         Default: 50 (log rows)

Example:
  PROJECT_ID="wayonagio-agente-ia-email" scripts/cloud/oauth_health_check.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_cmd gcloud
set_defaults
require_env PROJECT_ID

: "${LIMIT:=50}"

info "Service and revision configuration"
gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='yaml(status.latestReadyRevisionName,status.url,spec.template.spec.containers[0].env)'

printf '\n'
info "Recent OAuth/auth-related logs"
gcloud logging read \
  "resource.type=\"cloud_run_revision\" \
AND resource.labels.service_name=\"${SERVICE_NAME}\" \
AND (textPayload:\"OAuth token refresh failed\" \
  OR textPayload:\"invalid_grant\" \
  OR textPayload:\"Authentication successful\" \
  OR textPayload:\"POST /draft-reply\")" \
  --project="${PROJECT_ID}" \
  --limit="${LIMIT}" \
  --order=desc \
  --format='table(timestamp,severity,textPayload)'
