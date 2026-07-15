#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/iron_agent_smoke_common.sh
source "${SCRIPT_DIR}/lib/iron_agent_smoke_common.sh"

ASSISTANT_ID="${ASSISTANT_ID:-}"

init_iron_agent_smoke "verify-assistant-files"

log "Log file: ${LOG_FILE}"
log "This script does not print secret values."

check_basic_dependencies
require_assistant_id
check_namespace
load_required_secrets
ensure_gateway_access
print_readyz
maybe_auto_start_assistant
wait_for_assistant_pod_ready
verify_files_route_for_assistant

set_step "final health check"
print_readyz

log "Assistant file upload/download route completed."
print_route_summary
