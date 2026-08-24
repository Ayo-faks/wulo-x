#!/bin/bash
# ============================================================================
# 📦 App Configuration Sync
# ============================================================================
# Syncs infrastructure keys from azd env to Azure App Configuration.
# This script syncs values that are only known after Terraform provisioning
# (e.g., service endpoints, container URLs).
#
# Usage: ./sync-appconfig.sh [--endpoint URL] [--label LABEL] [--dry-run]
# ============================================================================

set -euo pipefail

# ============================================================================
# Logging
# ============================================================================

if [[ -z "${BLUE+x}" ]]; then BLUE=$'\033[0;34m'; fi
if [[ -z "${GREEN+x}" ]]; then GREEN=$'\033[0;32m'; fi
if [[ -z "${GREEN_BOLD+x}" ]]; then GREEN_BOLD=$'\033[1;32m'; fi
if [[ -z "${YELLOW+x}" ]]; then YELLOW=$'\033[1;33m'; fi
if [[ -z "${RED+x}" ]]; then RED=$'\033[0;31m'; fi
if [[ -z "${DIM+x}" ]]; then DIM=$'\033[2m'; fi
if [[ -z "${NC+x}" ]]; then NC=$'\033[0m'; fi
readonly BLUE GREEN GREEN_BOLD YELLOW RED DIM NC

log()          { printf '│ %s%s%s\n' "$DIM" "$*" "$NC"; }
info()         { printf '│ %s%s%s\n' "$BLUE" "$*" "$NC"; }
success()      { printf '│ %s✔%s %s\n' "$GREEN" "$NC" "$*"; }
phase_success(){ printf '│ %s✔ %s%s\n' "$GREEN_BOLD" "$*" "$NC"; }
warn()         { printf '│ %s⚠%s  %s\n' "$YELLOW" "$NC" "$*"; }
fail()         { printf '│ %s✖%s %s\n' "$RED" "$NC" "$*" >&2; }

# ============================================================================
# Parse Arguments
# ============================================================================

ENDPOINT=""
LABEL=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --endpoint) ENDPOINT="$2"; shift 2 ;;
        --label) LABEL="$2"; shift 2 ;;
        --config) shift 2 ;; # Ignored for backward compatibility
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--endpoint URL] [--label LABEL] [--dry-run]"
            exit 0
            ;;
        *) fail "Unknown option: $1"; exit 1 ;;
    esac
done

# Get from azd env if not provided
if [[ -z "$ENDPOINT" ]]; then
    ENDPOINT=$(azd env get-value AZURE_APPCONFIG_ENDPOINT 2>/dev/null || echo "")
fi
if [[ -z "$LABEL" ]]; then
    LABEL=$(azd env get-value AZURE_ENV_NAME 2>/dev/null || echo "")
fi

if [[ -z "$ENDPOINT" ]]; then
    fail "App Config endpoint not set. Use --endpoint or set AZURE_APPCONFIG_ENDPOINT"
    exit 1
fi

# Validate endpoint format
if [[ ! "$ENDPOINT" =~ \.azconfig\.io$ ]]; then
    fail "Invalid App Configuration endpoint format: $ENDPOINT"
    fail "Expected format: https://<name>.azconfig.io"
    exit 1
fi

# ============================================================================
# Helper Functions
# ============================================================================

# Helper to get azd env value
get_azd_value() {
    local value
    value=$(azd env get-value "$1" 2>/dev/null | tail -1 || echo "")
    if [[ -z "$value" || "$value" == ERROR* || "$value" == "Suggestion:"* || "$value" == Update\ available:* ]]; then
        echo ""
        return 0
    fi
    echo "$value"
}

# Helper to set a key-value in App Config
set_kv() {
    local key="$1" value="$2" content_type="${3:-}"
    
    # Skip empty values
    [[ -z "$value" ]] && return 0
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [DRY-RUN] $key = ${value:0:50}..."
        return 0
    fi
    
    local cmd_args=(
        --endpoint "$ENDPOINT"
        --key "$key"
        --value "$value"
        --auth-mode login
        --yes
        --output none
    )
    [[ -n "$LABEL" ]] && cmd_args+=(--label "$LABEL")
    [[ -n "$content_type" ]] && cmd_args+=(--content-type "$content_type")
    
    local error_output
    if error_output=$(az appconfig kv set "${cmd_args[@]}" 2>&1); then
        return 0
    else
        fail "Failed to set key: $key"
        log "  └─ Value attempted: ${value:0:100}..."
        # Show the full error message for debugging
        local error_msg
        error_msg=$(echo "$error_output" | head -3)
        [[ -n "$error_msg" ]] && log "  └─ Error: $error_msg"
        return 1
    fi
}

# Helper to remove one stale App Config key without touching its Key Vault secret.
delete_kv() {
    local key="$1"

    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [DRY-RUN] delete $key"
        return 0
    fi
    if [[ -z "$(get_appconfig_value "$key")" ]]; then
        return 0
    fi

    local cmd_args=(
        --endpoint "$ENDPOINT"
        --key "$key"
        --auth-mode login
        --yes
        --output none
    )
    [[ -n "$LABEL" ]] && cmd_args+=(--label "$LABEL")
    if az appconfig kv delete "${cmd_args[@]}" >/dev/null 2>&1; then
        return 0
    fi
    fail "Failed to delete key: $key"
    return 1
}

# Helper to get existing App Config value (for preserving values not in azd env)
get_appconfig_value() {
    local key="$1"
    local label_arg=""
    [[ -n "$LABEL" ]] && label_arg="--label $LABEL"
    
    # shellcheck disable=SC2086
    az appconfig kv show \
        --endpoint "$ENDPOINT" \
        --key "$key" \
        $label_arg \
        --auth-mode login \
        --query value \
        --output tsv 2>/dev/null || echo ""
}

# Helper to add Key Vault reference
set_kv_ref() {
    local key="$1" secret_name="$2"
    local kv_uri
    kv_uri=$(get_azd_value AZURE_KEY_VAULT_ENDPOINT)
    
    [[ -z "$kv_uri" ]] && return 0
    
    local ref_value="{\"uri\":\"${kv_uri}secrets/${secret_name}\"}"
    set_kv "$key" "$ref_value" "application/vnd.microsoft.appconfig.keyvaultref+json;charset=utf-8"
}

# ============================================================================
# Main
# ============================================================================

echo ""
echo "╭─────────────────────────────────────────────────────────────"
echo "│ 📦 App Configuration Sync"
echo "├─────────────────────────────────────────────────────────────"
info "Endpoint: $ENDPOINT"
info "Label: ${LABEL:-<none>}"
[[ "$DRY_RUN" == "true" ]] && warn "DRY RUN - no changes will be made"
echo "├─────────────────────────────────────────────────────────────"

# ============================================================================
# Sync Infrastructure Keys from azd env
# ============================================================================
log ""
log "Syncing infrastructure keys from azd env..."

count=0
errors=()
audit_db_configuration_failed=false

# Azure OpenAI
set_kv "azure/openai/endpoint" "$(get_azd_value AZURE_OPENAI_ENDPOINT)" && ((++count)) || errors+=("azure/openai/endpoint")
set_kv "azure/openai/deployment-id" "$(get_azd_value AZURE_OPENAI_CHAT_DEPLOYMENT_ID)" && ((++count)) || errors+=("azure/openai/deployment-id")
set_kv "azure/openai/api-version" "$(get_azd_value AZURE_OPENAI_API_VERSION)" && ((++count)) || errors+=("azure/openai/api-version")

# Azure Speech
set_kv "azure/speech/endpoint" "$(get_azd_value AZURE_SPEECH_ENDPOINT)" && ((++count)) || errors+=("azure/speech/endpoint")
set_kv "azure/speech/region" "$(get_azd_value AZURE_SPEECH_REGION)" && ((++count)) || errors+=("azure/speech/region")
set_kv "azure/speech/resource-id" "$(get_azd_value AZURE_SPEECH_RESOURCE_ID)" && ((++count)) || errors+=("azure/speech/resource-id")

# Azure Communication Services
set_kv "azure/acs/endpoint" "$(get_azd_value ACS_ENDPOINT)" && ((++count)) || errors+=("azure/acs/endpoint")
set_kv "azure/acs/auth-mode" "${ACS_AUTH_MODE:-entra}" && ((++count)) || errors+=("azure/acs/auth-mode")
set_kv "azure/acs/immutable-id" "$(get_azd_value ACS_IMMUTABLE_ID)" && ((++count)) || errors+=("azure/acs/immutable-id")
# NOTE: ACS now authenticates Call Automation via the backend user-assigned managed
# identity (Microsoft Entra ID), not the access-key connection string. The app uses
# the endpoint + managed identity when ACS_CONNECTION_STRING is absent. We therefore
# intentionally DO NOT sync azure/acs/connection-string. If you ever revert to
# access-key auth, re-enable the line below AND grant the backend UAMI access removed.
# set_kv_ref "azure/acs/connection-string" "acs-connection-string" && ((++count)) || errors+=("azure/acs/connection-string")
set_kv "azure/acs/email-sender-address" "$(get_azd_value AZURE_EMAIL_SENDER_ADDRESS)" && ((++count)) || errors+=("azure/acs/email-sender-address")

# SMS provider fallback (Phase 0). ACS remains default; set SMS_PROVIDER=twilio only when
# ACS cannot provide a suitable SMS-capable number for the spike.
sms_provider="${SMS_PROVIDER:-$(get_azd_value SMS_PROVIDER)}"
twilio_account_sid="${TWILIO_ACCOUNT_SID:-$(get_azd_value TWILIO_ACCOUNT_SID)}"
twilio_from_phone_number="${TWILIO_FROM_PHONE_NUMBER:-$(get_azd_value TWILIO_FROM_PHONE_NUMBER)}"
twilio_webhook_base_url="${TWILIO_WEBHOOK_BASE_URL:-$(get_azd_value TWILIO_WEBHOOK_BASE_URL)}"
twilio_status_callback_url="${TWILIO_SMS_STATUS_CALLBACK_URL:-$(get_azd_value TWILIO_SMS_STATUS_CALLBACK_URL)}"

if [[ -n "$sms_provider" ]]; then
    set_kv "app/sms/provider" "$sms_provider" && ((++count)) || errors+=("app/sms/provider")
fi
if [[ -n "$twilio_account_sid" ]]; then
    set_kv "app/sms/twilio/account-sid" "$twilio_account_sid" && ((++count)) || errors+=("app/sms/twilio/account-sid")
fi
if [[ -n "$twilio_from_phone_number" ]]; then
    set_kv "app/sms/twilio/from-phone-number" "$twilio_from_phone_number" && ((++count)) || errors+=("app/sms/twilio/from-phone-number")
fi
if [[ -n "$twilio_webhook_base_url" ]]; then
    set_kv "app/sms/twilio/webhook-base-url" "$twilio_webhook_base_url" && ((++count)) || errors+=("app/sms/twilio/webhook-base-url")
fi
if [[ -n "$twilio_status_callback_url" ]]; then
    set_kv "app/sms/twilio/status-callback-url" "$twilio_status_callback_url" && ((++count)) || errors+=("app/sms/twilio/status-callback-url")
fi

# Durable CALL dispatch is one non-secret operational gate. Missing or unknown
# values remain false in the runtime configuration parser.
durable_call_enabled="${CLINIC_RECALL_DURABLE_CALL_ENABLED:-$(get_azd_value CLINIC_RECALL_DURABLE_CALL_ENABLED)}"
durable_call_provider="${CLINIC_RECALL_DURABLE_CALL_PROVIDER:-$(get_azd_value CLINIC_RECALL_DURABLE_CALL_PROVIDER)}"
if [[ -n "$durable_call_enabled" ]]; then
    durable_call_enabled="${durable_call_enabled,,}"
    if [[ "$durable_call_enabled" != "true" && "$durable_call_enabled" != "false" ]]; then
        errors+=("app/clinic-recall/durable-call-enabled must be true or false")
    else
        set_kv "app/clinic-recall/durable-call-enabled" "$durable_call_enabled" && ((++count)) || errors+=("app/clinic-recall/durable-call-enabled")
    fi
fi
if [[ -n "$durable_call_provider" ]]; then
    durable_call_provider="${durable_call_provider,,}"
    if [[ "$durable_call_provider" != "twilio" ]]; then
        errors+=("app/clinic-recall/durable-call-provider must be twilio")
    else
        set_kv "app/clinic-recall/durable-call-provider" "$durable_call_provider" && ((++count)) || errors+=("app/clinic-recall/durable-call-provider")
    fi
fi

# Durable recording dispatch is independent and remains false unless explicitly set.
durable_recording_enabled="${CLINIC_RECALL_DURABLE_RECORDING_ENABLED:-$(get_azd_value CLINIC_RECALL_DURABLE_RECORDING_ENABLED)}"
durable_recording_provider="${CLINIC_RECALL_DURABLE_RECORDING_PROVIDER:-$(get_azd_value CLINIC_RECALL_DURABLE_RECORDING_PROVIDER)}"
if [[ -n "$durable_recording_enabled" ]]; then
    durable_recording_enabled="${durable_recording_enabled,,}"
    if [[ "$durable_recording_enabled" != "true" && "$durable_recording_enabled" != "false" ]]; then
        errors+=("app/clinic-recall/durable-recording-enabled must be true or false")
    else
        set_kv "app/clinic-recall/durable-recording-enabled" "$durable_recording_enabled" && ((++count)) || errors+=("app/clinic-recall/durable-recording-enabled")
    fi
fi
if [[ -n "$durable_recording_provider" ]]; then
    durable_recording_provider="${durable_recording_provider,,}"
    if [[ "$durable_recording_provider" != "twilio" ]]; then
        errors+=("app/clinic-recall/durable-recording-provider must be twilio")
    else
        set_kv "app/clinic-recall/durable-recording-provider" "$durable_recording_provider" && ((++count)) || errors+=("app/clinic-recall/durable-recording-provider")
    fi
fi

# PR-10 rights and retention configuration. Destructive activation switches are
# Job-local environment gates and are intentionally not written to App Config.
rights_twilio_enabled="${CLINIC_RECALL_DURABLE_RIGHTS_TWILIO_ENABLED:-$(get_azd_value CLINIC_RECALL_DURABLE_RIGHTS_TWILIO_ENABLED)}"
rights_blob_enabled="${CLINIC_RECALL_DURABLE_RIGHTS_BLOB_ENABLED:-$(get_azd_value CLINIC_RECALL_DURABLE_RIGHTS_BLOB_ENABLED)}"
rights_twilio_enabled="${rights_twilio_enabled,,}"
rights_blob_enabled="${rights_blob_enabled,,}"
rights_hmac_key_version="${CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION:-$(get_azd_value CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION)}"
rights_policy_version="${CLINIC_RECALL_RIGHTS_POLICY_VERSION:-$(get_azd_value CLINIC_RECALL_RIGHTS_POLICY_VERSION)}"
rights_approval_hash="${CLINIC_RECALL_RIGHTS_APPROVAL_EVIDENCE_SHA256:-$(get_azd_value CLINIC_RECALL_RIGHTS_APPROVAL_EVIDENCE_SHA256)}"
rights_request_due_seconds="${CLINIC_RECALL_RIGHTS_REQUEST_DUE_SECONDS:-$(get_azd_value CLINIC_RECALL_RIGHTS_REQUEST_DUE_SECONDS)}"
rights_residual_approvals_json="${CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON:-$(get_azd_value CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON)}"
retention_policy_version="${CLINIC_RECALL_RETENTION_POLICY_VERSION:-$(get_azd_value CLINIC_RECALL_RETENTION_POLICY_VERSION)}"
retention_approval_hash="${CLINIC_RECALL_RETENTION_APPROVAL_EVIDENCE_SHA256:-$(get_azd_value CLINIC_RECALL_RETENTION_APPROVAL_EVIDENCE_SHA256)}"
retention_policy_approved_at="${CLINIC_RECALL_RETENTION_POLICY_APPROVED_AT:-$(get_azd_value CLINIC_RECALL_RETENTION_POLICY_APPROVED_AT)}"
retention_policy_effective_at="${CLINIC_RECALL_RETENTION_POLICY_EFFECTIVE_AT:-$(get_azd_value CLINIC_RECALL_RETENTION_POLICY_EFFECTIVE_AT)}"
retention_policy_expires_at="${CLINIC_RECALL_RETENTION_POLICY_EXPIRES_AT:-$(get_azd_value CLINIC_RECALL_RETENTION_POLICY_EXPIRES_AT)}"
retention_retain_for_seconds="${CLINIC_RECALL_RETENTION_RETAIN_FOR_SECONDS:-$(get_azd_value CLINIC_RECALL_RETENTION_RETAIN_FOR_SECONDS)}"
retention_request_due_seconds="${CLINIC_RECALL_RETENTION_REQUEST_DUE_SECONDS:-$(get_azd_value CLINIC_RECALL_RETENTION_REQUEST_DUE_SECONDS)}"

for rights_switch in rights_twilio_enabled rights_blob_enabled; do
    rights_switch_value="${!rights_switch,,}"
    if [[ -n "$rights_switch_value" && "$rights_switch_value" != "true" && "$rights_switch_value" != "false" ]]; then
        errors+=("$rights_switch must be true or false")
    fi
done
if [[ "$rights_twilio_enabled" == "true" || "$rights_twilio_enabled" == "false" ]]; then
    set_kv "app/clinic-recall/rights/twilio-enabled" "$rights_twilio_enabled" && ((++count)) || errors+=("app/clinic-recall/rights/twilio-enabled")
fi
if [[ "$rights_blob_enabled" == "true" || "$rights_blob_enabled" == "false" ]]; then
    set_kv "app/clinic-recall/rights/blob-enabled" "$rights_blob_enabled" && ((++count)) || errors+=("app/clinic-recall/rights/blob-enabled")
fi

for version_entry in \
    "rights_hmac_key_version:app/clinic-recall/rights/hmac-key-version" \
    "rights_policy_version:app/clinic-recall/rights/policy-version" \
    "retention_policy_version:app/clinic-recall/retention/policy-version"; do
    variable_name="${version_entry%%:*}"
    appconfig_key="${version_entry#*:}"
    version_value="${!variable_name}"
    if [[ -n "$version_value" ]]; then
        if [[ ! "$version_value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
            errors+=("$appconfig_key is invalid")
        else
            set_kv "$appconfig_key" "$version_value" && ((++count)) || errors+=("$appconfig_key")
        fi
    fi
done

if [[ -n "$rights_residual_approvals_json" ]]; then
    if ! RIGHTS_RESIDUAL_APPROVALS_JSON="$rights_residual_approvals_json" python3 -c '
import datetime
import json
import os
import re

expected = {"policy_version", "approval_evidence_sha256", "due_at", "completion_eligible"}
allowed_categories = {
    "provider_backup_window", "provider_metadata_window", "blob_soft_delete_window",
    "legal_or_immutability_hold", "processor_procedure", "legacy_archive_procedure",
    "cliniko_controller_procedure", "postgres_backup_window", "application_log_window",
    "monitor_log_window", "support_procedure", "voice_live_processor_procedure",
    "redis_session_procedure", "clinical_governance_record",
}
payload = json.loads(os.environ["RIGHTS_RESIDUAL_APPROVALS_JSON"])
if not isinstance(payload, dict) or not payload:
    raise ValueError("approval payload must be a non-empty object")
for category, approval in payload.items():
    if category not in allowed_categories:
        raise ValueError("unknown residual category")
    if not isinstance(approval, dict) or set(approval) != expected:
        raise ValueError("invalid approval fields")
    if not isinstance(approval["policy_version"], str) or not approval["policy_version"]:
        raise ValueError("invalid policy version")
    if not re.fullmatch(r"[0-9a-f]{64}", approval["approval_evidence_sha256"]):
        raise ValueError("invalid approval evidence hash")
    if not isinstance(approval["completion_eligible"], bool):
        raise ValueError("invalid completion eligibility")
    due_at = datetime.datetime.fromisoformat(approval["due_at"].replace("Z", "+00:00"))
    if due_at.tzinfo is None or due_at.utcoffset() is None:
        raise ValueError("due_at must be timezone-aware")
' >/dev/null 2>&1; then
        errors+=("app/clinic-recall/rights/residual-approvals-json is invalid")
    else
        set_kv "app/clinic-recall/rights/residual-approvals-json" "$rights_residual_approvals_json" && ((++count)) || errors+=("app/clinic-recall/rights/residual-approvals-json")
    fi
fi

for hash_entry in \
    "rights_approval_hash:app/clinic-recall/rights/approval-evidence-sha256" \
    "retention_approval_hash:app/clinic-recall/retention/approval-evidence-sha256"; do
    variable_name="${hash_entry%%:*}"
    appconfig_key="${hash_entry#*:}"
    hash_value="${!variable_name}"
    if [[ -n "$hash_value" ]]; then
        if [[ ! "$hash_value" =~ ^[0-9a-f]{64}$ ]]; then
            errors+=("$appconfig_key must be a lowercase SHA-256 digest")
        else
            set_kv "$appconfig_key" "$hash_value" && ((++count)) || errors+=("$appconfig_key")
        fi
    fi
done

for seconds_entry in \
    "rights_request_due_seconds:app/clinic-recall/rights/request-due-seconds" \
    "retention_retain_for_seconds:app/clinic-recall/retention/retain-for-seconds" \
    "retention_request_due_seconds:app/clinic-recall/retention/request-due-seconds"; do
    variable_name="${seconds_entry%%:*}"
    appconfig_key="${seconds_entry#*:}"
    seconds_value="${!variable_name}"
    if [[ -n "$seconds_value" ]]; then
        if [[ ! "$seconds_value" =~ ^[0-9]+$ ]] || (( seconds_value < 1 )); then
            errors+=("$appconfig_key must be a positive integer")
        else
            set_kv "$appconfig_key" "$seconds_value" && ((++count)) || errors+=("$appconfig_key")
        fi
    fi
done

for timestamp_entry in \
    "retention_policy_approved_at:app/clinic-recall/retention/policy-approved-at" \
    "retention_policy_effective_at:app/clinic-recall/retention/policy-effective-at" \
    "retention_policy_expires_at:app/clinic-recall/retention/policy-expires-at"; do
    variable_name="${timestamp_entry%%:*}"
    appconfig_key="${timestamp_entry#*:}"
    timestamp_value="${!variable_name}"
    if [[ -n "$timestamp_value" ]]; then
        if [[ ! "$timestamp_value" =~ (Z|[+-][0-9]{2}:[0-9]{2})$ ]] || ! date -u -d "$timestamp_value" +%s >/dev/null 2>&1; then
            errors+=("$appconfig_key must be timezone-aware RFC3339")
        else
            set_kv "$appconfig_key" "$timestamp_value" && ((++count)) || errors+=("$appconfig_key")
        fi
    fi
done

key_vault_name="$(get_azd_value AZURE_KEY_VAULT_NAME)"
if [[ -n "$key_vault_name" ]]; then
    if az keyvault secret show --vault-name "$key_vault_name" --name clinic-recall-rights-hmac-key --query id --output tsv >/dev/null 2>&1; then
        set_kv_ref "app/clinic-recall/rights/hmac-key" "clinic-recall-rights-hmac-key" && ((++count)) || errors+=("app/clinic-recall/rights/hmac-key")
    else
        warn "Clinic Recall rights HMAC key is absent from Key Vault; rights and retention runtime stay blocked"
    fi
    if az keyvault secret show --vault-name "$key_vault_name" --name clinic-recall-rights-hmac-previous-keys-json --query id --output tsv >/dev/null 2>&1; then
        set_kv_ref "app/clinic-recall/rights/hmac-previous-keys-json" "clinic-recall-rights-hmac-previous-keys-json" && ((++count)) || errors+=("app/clinic-recall/rights/hmac-previous-keys-json")
    fi
fi

# Clinic Recall Cliniko sync (PR-05)
# The gate is written false first and can become true only after every
# non-secret setting and the existing Key Vault reference are synchronized.
cliniko_configuration_failed=false
cliniko_enabled_raw="${CLINIC_RECALL_CLINIKO_SYNC_ENABLED:-$(get_azd_value CLINIC_RECALL_CLINIKO_SYNC_ENABLED)}"
cliniko_enabled="${cliniko_enabled_raw,,}"
cliniko_write_enabled_raw="${CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED:-$(get_azd_value CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED)}"
cliniko_write_enabled="${cliniko_write_enabled_raw,,}"
cliniko_reconciliation_enabled_raw="${CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED:-$(get_azd_value CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED)}"
cliniko_reconciliation_enabled="${cliniko_reconciliation_enabled_raw,,}"
booking_confirmation_enabled_raw="${CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED:-$(get_azd_value CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED)}"
booking_confirmation_enabled="${booking_confirmation_enabled_raw,,}"

if ! set_kv "app/clinic-recall/cliniko/enabled" "false"; then
    fail "Cannot establish the default-off Cliniko gate"
    exit 1
fi
((++count))
if ! set_kv "app/clinic-recall/cliniko/write-enabled" "false"; then
    fail "Cannot establish the default-off Cliniko write gate"
    exit 1
fi
((++count))
if ! set_kv "app/clinic-recall/cliniko/reconciliation-enabled" "false"; then
    fail "Cannot establish the default-off Cliniko reconciliation gate"
    exit 1
fi
((++count))
if ! set_kv "app/clinic-recall/booking-confirmation/enabled" "false"; then
    fail "Cannot establish the default-off booking confirmation gate"
    exit 1
fi
((++count))

if [[ -n "$cliniko_write_enabled" && "$cliniko_write_enabled" != "true" && "$cliniko_write_enabled" != "false" ]]; then
    errors+=("app/clinic-recall/cliniko/write-enabled must be true or false")
    cliniko_configuration_failed=true
fi
if [[ -n "$cliniko_reconciliation_enabled" && "$cliniko_reconciliation_enabled" != "true" && "$cliniko_reconciliation_enabled" != "false" ]]; then
    errors+=("app/clinic-recall/cliniko/reconciliation-enabled must be true or false")
    cliniko_configuration_failed=true
fi
if [[ -n "$booking_confirmation_enabled" && "$booking_confirmation_enabled" != "true" && "$booking_confirmation_enabled" != "false" ]]; then
    errors+=("app/clinic-recall/booking-confirmation/enabled must be true or false")
    cliniko_configuration_failed=true
fi
if [[ "$cliniko_enabled" != "true" ]] && {
    [[ "$cliniko_write_enabled" == "true" ]] ||
    [[ "$cliniko_reconciliation_enabled" == "true" ]] ||
    [[ "$booking_confirmation_enabled" == "true" ]]
}; then
    errors+=("Cliniko write, reconciliation, and confirmation require app/clinic-recall/cliniko/enabled")
    cliniko_configuration_failed=true
fi

if [[ -n "$cliniko_enabled" && "$cliniko_enabled" != "true" && "$cliniko_enabled" != "false" ]]; then
    errors+=("app/clinic-recall/cliniko/enabled must be true or false")
    cliniko_configuration_failed=true
elif [[ "$cliniko_enabled" == "true" ]]; then
    cliniko_shard="${CLINIC_RECALL_CLINIKO_SHARD:-$(get_azd_value CLINIC_RECALL_CLINIKO_SHARD)}"
    cliniko_user_agent="${CLINIC_RECALL_CLINIKO_USER_AGENT:-$(get_azd_value CLINIC_RECALL_CLINIKO_USER_AGENT)}"
    cliniko_timeout_seconds="${CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS:-$(get_azd_value CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS)}"
    cliniko_per_page="${CLINIC_RECALL_CLINIKO_PER_PAGE:-$(get_azd_value CLINIC_RECALL_CLINIKO_PER_PAGE)}"
    cliniko_max_pages="${CLINIC_RECALL_CLINIKO_MAX_PAGES:-$(get_azd_value CLINIC_RECALL_CLINIKO_MAX_PAGES)}"
    cliniko_max_items="${CLINIC_RECALL_CLINIKO_MAX_ITEMS:-$(get_azd_value CLINIC_RECALL_CLINIKO_MAX_ITEMS)}"
    cliniko_key_vault_endpoint="$(get_azd_value AZURE_KEY_VAULT_ENDPOINT)"

    if [[ ! "$cliniko_shard" =~ ^(uk1|uk2|uk3)$ ]]; then
        errors+=("app/clinic-recall/cliniko/shard must be uk1, uk2, or uk3")
        cliniko_configuration_failed=true
    fi
    if [[ ! "$cliniko_user_agent" =~ ^[^()]+\ \([^()[:space:]@]+@[^()[:space:]@]+\.[^()[:space:]@]+\)$ ]]; then
        errors+=("app/clinic-recall/cliniko/user-agent is invalid")
        cliniko_configuration_failed=true
    fi
    if [[ -n "$cliniko_timeout_seconds" ]] && { [[ ! "$cliniko_timeout_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk -v value="$cliniko_timeout_seconds" 'BEGIN { exit !(value >= 1 && value <= 30) }'; }; then
        errors+=("app/clinic-recall/cliniko/timeout-seconds must be between 1 and 30")
        cliniko_configuration_failed=true
    fi
    for cliniko_bound in \
        "cliniko_per_page:app/clinic-recall/cliniko/per-page:100" \
        "cliniko_max_pages:app/clinic-recall/cliniko/max-pages:100" \
        "cliniko_max_items:app/clinic-recall/cliniko/max-items:10000"; do
        variable_name="${cliniko_bound%%:*}"
        remainder="${cliniko_bound#*:}"
        appconfig_key="${remainder%:*}"
        maximum="${cliniko_bound##*:}"
        value="${!variable_name}"
        if [[ -n "$value" ]] && { [[ ! "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > maximum )); }; then
            errors+=("$appconfig_key is outside its allowed bounds")
            cliniko_configuration_failed=true
        fi
    done
    if [[ -z "$key_vault_name" || -z "$cliniko_key_vault_endpoint" ]]; then
        errors+=("clinic-recall-cliniko-api-key Key Vault reference is unavailable")
        cliniko_configuration_failed=true
    elif [[ "$DRY_RUN" != "true" ]] && ! az keyvault secret show --vault-name "$key_vault_name" --name clinic-recall-cliniko-api-key --query id --output tsv >/dev/null 2>&1; then
        errors+=("clinic-recall-cliniko-api-key is absent from Key Vault")
        cliniko_configuration_failed=true
    fi

    if [[ "$cliniko_configuration_failed" != "true" ]]; then
        for cliniko_setting in \
            "app/clinic-recall/cliniko/shard:$cliniko_shard" \
            "app/clinic-recall/cliniko/user-agent:$cliniko_user_agent" \
            "app/clinic-recall/cliniko/timeout-seconds:$cliniko_timeout_seconds" \
            "app/clinic-recall/cliniko/per-page:$cliniko_per_page" \
            "app/clinic-recall/cliniko/max-pages:$cliniko_max_pages" \
            "app/clinic-recall/cliniko/max-items:$cliniko_max_items"; do
            appconfig_key="${cliniko_setting%%:*}"
            value="${cliniko_setting#*:}"
            if [[ -n "$value" ]]; then
                if set_kv "$appconfig_key" "$value"; then
                    ((++count))
                else
                    errors+=("$appconfig_key")
                    cliniko_configuration_failed=true
                fi
            fi
        done
    fi
    if [[ "$cliniko_configuration_failed" != "true" ]]; then
        if set_kv_ref "app/clinic-recall/cliniko/api-key" "clinic-recall-cliniko-api-key"; then
            ((++count))
        else
            errors+=("app/clinic-recall/cliniko/api-key")
            cliniko_configuration_failed=true
        fi
    fi
    if [[ "$cliniko_configuration_failed" != "true" ]]; then
        if set_kv "app/clinic-recall/cliniko/enabled" "true"; then
            ((++count))
        else
            errors+=("app/clinic-recall/cliniko/enabled")
            cliniko_configuration_failed=true
        fi
    fi
    if [[ "$cliniko_configuration_failed" != "true" && "$cliniko_write_enabled" == "true" ]]; then
        if set_kv "app/clinic-recall/cliniko/write-enabled" "true"; then
            ((++count))
        else
            errors+=("app/clinic-recall/cliniko/write-enabled")
            cliniko_configuration_failed=true
        fi
    fi
    if [[ "$cliniko_configuration_failed" != "true" && "$cliniko_reconciliation_enabled" == "true" ]]; then
        if set_kv "app/clinic-recall/cliniko/reconciliation-enabled" "true"; then
            ((++count))
        else
            errors+=("app/clinic-recall/cliniko/reconciliation-enabled")
            cliniko_configuration_failed=true
        fi
    fi
    if [[ "$cliniko_configuration_failed" != "true" && "$booking_confirmation_enabled" == "true" ]]; then
        if set_kv "app/clinic-recall/booking-confirmation/enabled" "true"; then
            ((++count))
        else
            errors+=("app/clinic-recall/booking-confirmation/enabled")
            cliniko_configuration_failed=true
        fi
    fi
else
    if ! delete_kv "app/clinic-recall/cliniko/api-key"; then
        errors+=("app/clinic-recall/cliniko/api-key")
        cliniko_configuration_failed=true
    fi
    warn "Clinic Recall Cliniko sync is disabled; CSV remains the ingestion path"
fi
if [[ "$cliniko_configuration_failed" == "true" ]]; then
    set_kv "app/clinic-recall/cliniko/enabled" "false" || true
    set_kv "app/clinic-recall/cliniko/write-enabled" "false" || true
    set_kv "app/clinic-recall/cliniko/reconciliation-enabled" "false" || true
    set_kv "app/clinic-recall/booking-confirmation/enabled" "false" || true
    delete_kv "app/clinic-recall/cliniko/api-key" || true
fi
# End Clinic Recall Cliniko sync

# Pilot operational controls are non-secret and false unless explicitly set.
# Refresh evidence is generated by the runtime only after a successful load.
pilot_outreach_enabled="${CLINIC_RECALL_PILOT_OUTREACH_ENABLED:-$(get_azd_value CLINIC_RECALL_PILOT_OUTREACH_ENABLED)}"
pilot_voice_enabled="${CLINIC_RECALL_PILOT_VOICE_ENABLED:-$(get_azd_value CLINIC_RECALL_PILOT_VOICE_ENABLED)}"
pilot_recording_enabled="${CLINIC_RECALL_PILOT_RECORDING_ENABLED:-$(get_azd_value CLINIC_RECALL_PILOT_RECORDING_ENABLED)}"
pilot_config_max_age_seconds="${CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS:-$(get_azd_value CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS)}"
pilot_environment="${CLINIC_RECALL_PILOT_ENVIRONMENT:-$(get_azd_value CLINIC_RECALL_PILOT_ENVIRONMENT)}"
pilot_release_identity="${CLINIC_RECALL_PILOT_RELEASE_IDENTITY:-$(get_azd_value CLINIC_RECALL_PILOT_RELEASE_IDENTITY)}"

for pilot_switch in pilot_outreach_enabled pilot_voice_enabled pilot_recording_enabled; do
    pilot_value="${!pilot_switch,,}"
    if [[ -n "$pilot_value" && "$pilot_value" != "true" && "$pilot_value" != "false" ]]; then
        errors+=("$pilot_switch must be true or false")
    fi
done
if [[ "$pilot_outreach_enabled" == "true" || "$pilot_outreach_enabled" == "false" ]]; then
    set_kv "app/clinic-recall/pilot/outreach-enabled" "$pilot_outreach_enabled" && ((++count)) || errors+=("app/clinic-recall/pilot/outreach-enabled")
fi
if [[ "$pilot_voice_enabled" == "true" || "$pilot_voice_enabled" == "false" ]]; then
    set_kv "app/clinic-recall/pilot/voice-enabled" "$pilot_voice_enabled" && ((++count)) || errors+=("app/clinic-recall/pilot/voice-enabled")
fi
if [[ "$pilot_recording_enabled" == "true" || "$pilot_recording_enabled" == "false" ]]; then
    set_kv "app/clinic-recall/pilot/recording-enabled" "$pilot_recording_enabled" && ((++count)) || errors+=("app/clinic-recall/pilot/recording-enabled")
fi
if [[ -n "$pilot_config_max_age_seconds" ]]; then
    if [[ ! "$pilot_config_max_age_seconds" =~ ^[0-9]+$ ]] || (( pilot_config_max_age_seconds < 1 || pilot_config_max_age_seconds > 3600 )); then
        errors+=("app/clinic-recall/pilot/config-max-age-seconds must be between 1 and 3600")
    else
        set_kv "app/clinic-recall/pilot/config-max-age-seconds" "$pilot_config_max_age_seconds" && ((++count)) || errors+=("app/clinic-recall/pilot/config-max-age-seconds")
    fi
fi
if [[ -n "$pilot_environment" ]]; then
    set_kv "app/clinic-recall/pilot/environment" "$pilot_environment" && ((++count)) || errors+=("app/clinic-recall/pilot/environment")
fi
if [[ -n "$pilot_release_identity" ]]; then
    set_kv "app/clinic-recall/pilot/release-identity" "$pilot_release_identity" && ((++count)) || errors+=("app/clinic-recall/pilot/release-identity")
fi

# Recording disclosure is non-secret but requires an explicit approval flag,
# exact reviewed text, and a bounded immutable version. Missing values stay off.
recording_disclosure_approved="${CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED:-$(get_azd_value CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED)}"
recording_disclosure_text="${CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT:-$(get_azd_value CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT)}"
recording_disclosure_version="${CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION:-$(get_azd_value CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION)}"
if [[ -n "$recording_disclosure_approved" ]]; then
    recording_disclosure_approved="${recording_disclosure_approved,,}"
    if [[ "$recording_disclosure_approved" != "true" && "$recording_disclosure_approved" != "false" ]]; then
        errors+=("app/clinic-recall/recording/disclosure-approved must be true or false")
    else
        set_kv "app/clinic-recall/recording/disclosure-approved" "$recording_disclosure_approved" && ((++count)) || errors+=("app/clinic-recall/recording/disclosure-approved")
    fi
fi
if [[ -n "$recording_disclosure_text" ]]; then
    if (( ${#recording_disclosure_text} < 20 || ${#recording_disclosure_text} > 500 )); then
        errors+=("app/clinic-recall/recording/disclosure-text must contain 20 to 500 characters")
    else
        set_kv "app/clinic-recall/recording/disclosure-text" "$recording_disclosure_text" && ((++count)) || errors+=("app/clinic-recall/recording/disclosure-text")
    fi
fi
if [[ -n "$recording_disclosure_version" ]]; then
    if [[ ! "$recording_disclosure_version" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$ ]]; then
        errors+=("app/clinic-recall/recording/disclosure-version is invalid")
    else
        set_kv "app/clinic-recall/recording/disclosure-version" "$recording_disclosure_version" && ((++count)) || errors+=("app/clinic-recall/recording/disclosure-version")
    fi
fi
recordings_blob_account_url="${RECORDINGS_BLOB_ACCOUNT_URL:-$(get_azd_value RECORDINGS_BLOB_ACCOUNT_URL)}"
recordings_blob_container="${RECORDINGS_BLOB_CONTAINER:-$(get_azd_value RECORDINGS_BLOB_CONTAINER)}"
if [[ -n "$recordings_blob_account_url" ]]; then
    if [[ ! "$recordings_blob_account_url" =~ ^https://[^/]+/$ ]]; then
        errors+=("app/clinic-recall/recording/blob-account-url must be an HTTPS account URL")
    else
        set_kv "app/clinic-recall/recording/blob-account-url" "$recordings_blob_account_url" && ((++count)) || errors+=("app/clinic-recall/recording/blob-account-url")
    fi
fi
if [[ -n "$recordings_blob_container" ]]; then
    if [[ ! "$recordings_blob_container" =~ ^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$ ]]; then
        errors+=("app/clinic-recall/recording/blob-container is invalid")
    else
        set_kv "app/clinic-recall/recording/blob-container" "$recordings_blob_container" && ((++count)) || errors+=("app/clinic-recall/recording/blob-container")
    fi
fi

# Clinic Recall cadence planning gates are non-secret and fail closed when any
# required value is absent, malformed, future-dated, or stale at runtime.
cadence_planning_enabled="${CLINIC_RECALL_CADENCE_PLANNING_ENABLED:-$(get_azd_value CLINIC_RECALL_CADENCE_PLANNING_ENABLED)}"
cadence_config_refreshed_at="${CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT:-$(get_azd_value CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT)}"
cadence_config_max_age_seconds="${CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS:-$(get_azd_value CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS)}"

if [[ -n "$cadence_planning_enabled" ]]; then
    cadence_planning_enabled="${cadence_planning_enabled,,}"
    if [[ "$cadence_planning_enabled" != "true" && "$cadence_planning_enabled" != "false" ]]; then
        errors+=("app/clinic-recall/cadence-planning-enabled must be true or false")
    else
        set_kv "app/clinic-recall/cadence-planning-enabled" "$cadence_planning_enabled" && ((++count)) || errors+=("app/clinic-recall/cadence-planning-enabled")
    fi
fi
if [[ -n "$cadence_config_refreshed_at" ]]; then
    if ! date -u -d "$cadence_config_refreshed_at" +%s >/dev/null 2>&1; then
        errors+=("app/clinic-recall/cadence-config-refreshed-at must be RFC3339")
    else
        set_kv "app/clinic-recall/cadence-config-refreshed-at" "$cadence_config_refreshed_at" && ((++count)) || errors+=("app/clinic-recall/cadence-config-refreshed-at")
    fi
fi
if [[ -n "$cadence_config_max_age_seconds" ]]; then
    if [[ ! "$cadence_config_max_age_seconds" =~ ^[0-9]+$ ]] || (( cadence_config_max_age_seconds < 1 || cadence_config_max_age_seconds > 3600 )); then
        errors+=("app/clinic-recall/cadence-config-max-age-seconds must be between 1 and 3600")
    else
        set_kv "app/clinic-recall/cadence-config-max-age-seconds" "$cadence_config_max_age_seconds" && ((++count)) || errors+=("app/clinic-recall/cadence-config-max-age-seconds")
    fi
fi

# PR-12 operational handoff capabilities remain independently default-off.
for handoff_setting in \
    "CLINIC_RECALL_HANDOFF_NOTIFICATION_ENABLED:app/clinic-recall/handoff-notification-enabled" \
    "CLINIC_RECALL_HANDOFF_AGEING_ENABLED:app/clinic-recall/handoff-ageing-enabled" \
    "CLINIC_RECALL_HANDOFF_DELIVERY_CALLBACK_ENABLED:app/clinic-recall/handoff-delivery-callback-enabled"; do
    handoff_environment_name="${handoff_setting%%:*}"
    handoff_key="${handoff_setting#*:}"
    handoff_value="${!handoff_environment_name:-$(get_azd_value "$handoff_environment_name")}"
    if [[ -n "$handoff_value" ]]; then
        handoff_value="${handoff_value,,}"
        if [[ "$handoff_value" != "true" && "$handoff_value" != "false" ]]; then
            errors+=("$handoff_key must be true or false")
        else
            set_kv "$handoff_key" "$handoff_value" && ((++count)) || errors+=("$handoff_key")
        fi
    fi
done

# Voice provider fallback. Twilio Voice reuses TWILIO_FROM_PHONE_NUMBER by default so
# SMS + Voice can share one Twilio number; TWILIO_VOICE_FROM_NUMBER is only an override.
voice_provider="${VOICE_PROVIDER:-$(get_azd_value VOICE_PROVIDER)}"
twilio_voice_from_phone_number="${TWILIO_VOICE_FROM_NUMBER:-$(get_azd_value TWILIO_VOICE_FROM_NUMBER)}"
twilio_voice_twiml_url="${TWILIO_VOICE_TWIML_URL:-$(get_azd_value TWILIO_VOICE_TWIML_URL)}"
twilio_media_stream_url="${TWILIO_MEDIA_STREAM_URL:-$(get_azd_value TWILIO_MEDIA_STREAM_URL)}"
twilio_voice_inline_twiml="${TWILIO_VOICE_INLINE_TWIML:-$(get_azd_value TWILIO_VOICE_INLINE_TWIML)}"
twilio_voice_status_callback_url="${TWILIO_VOICE_STATUS_CALLBACK_URL:-$(get_azd_value TWILIO_VOICE_STATUS_CALLBACK_URL)}"

if [[ -n "$voice_provider" ]]; then
    set_kv "app/voice/provider" "$voice_provider" && ((++count)) || errors+=("app/voice/provider")
fi
if [[ -n "$twilio_voice_from_phone_number" ]]; then
    set_kv "app/voice/twilio/from-phone-number" "$twilio_voice_from_phone_number" && ((++count)) || errors+=("app/voice/twilio/from-phone-number")
fi
if [[ -n "$twilio_voice_twiml_url" ]]; then
    set_kv "app/voice/twilio/twiml-url" "$twilio_voice_twiml_url" && ((++count)) || errors+=("app/voice/twilio/twiml-url")
fi
if [[ -n "$twilio_media_stream_url" ]]; then
    set_kv "app/voice/twilio/media-stream-url" "$twilio_media_stream_url" && ((++count)) || errors+=("app/voice/twilio/media-stream-url")
fi
if [[ -n "$twilio_voice_inline_twiml" ]]; then
    set_kv "app/voice/twilio/inline-twiml" "$twilio_voice_inline_twiml" && ((++count)) || errors+=("app/voice/twilio/inline-twiml")
fi
if [[ -n "$twilio_voice_status_callback_url" ]]; then
    set_kv "app/voice/twilio/status-callback-url" "$twilio_voice_status_callback_url" && ((++count)) || errors+=("app/voice/twilio/status-callback-url")
fi

# Optional operator-maintained pricing inputs for aggregate estimated-cost telemetry.
# These are non-secret rates; no estimate is emitted unless both values are configured.
genai_input_cost_rate="${GENAI_INPUT_COST_PER_MILLION_TOKENS_USD:-$(get_azd_value GENAI_INPUT_COST_PER_MILLION_TOKENS_USD)}"
genai_output_cost_rate="${GENAI_OUTPUT_COST_PER_MILLION_TOKENS_USD:-$(get_azd_value GENAI_OUTPUT_COST_PER_MILLION_TOKENS_USD)}"
if [[ -n "$genai_input_cost_rate" && ! "$genai_input_cost_rate" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    errors+=("app/monitoring/genai-input-cost-per-million-tokens-usd must be non-negative numeric")
elif [[ -n "$genai_input_cost_rate" ]]; then
    set_kv "app/monitoring/genai-input-cost-per-million-tokens-usd" "$genai_input_cost_rate" && ((++count)) || errors+=("app/monitoring/genai-input-cost-per-million-tokens-usd")
fi
if [[ -n "$genai_output_cost_rate" && ! "$genai_output_cost_rate" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    errors+=("app/monitoring/genai-output-cost-per-million-tokens-usd must be non-negative numeric")
elif [[ -n "$genai_output_cost_rate" ]]; then
    set_kv "app/monitoring/genai-output-cost-per-million-tokens-usd" "$genai_output_cost_rate" && ((++count)) || errors+=("app/monitoring/genai-output-cost-per-million-tokens-usd")
fi

twilio_auth_token="${TWILIO_AUTH_TOKEN:-}"
key_vault_name="$(get_azd_value AZURE_KEY_VAULT_NAME)"
if [[ -n "$key_vault_name" ]]; then
    if [[ -n "$twilio_auth_token" && "$DRY_RUN" != "true" ]]; then
        az keyvault secret set \
            --vault-name "$key_vault_name" \
            --name twilio-auth-token \
            --value "$twilio_auth_token" \
            --output none >/dev/null
        set_kv_ref "app/sms/twilio/auth-token" "twilio-auth-token" && ((++count)) || errors+=("app/sms/twilio/auth-token")
    elif az keyvault secret show --vault-name "$key_vault_name" --name twilio-auth-token --query id --output tsv >/dev/null 2>&1; then
        set_kv_ref "app/sms/twilio/auth-token" "twilio-auth-token" && ((++count)) || errors+=("app/sms/twilio/auth-token")
    elif [[ "$sms_provider" == "twilio" || "$voice_provider" == "twilio" ]]; then
        warn "Twilio provider selected but twilio-auth-token is not in Key Vault and TWILIO_AUTH_TOKEN is not set"
    fi
fi

# Public demo experience (60-second landing-page demo). The Turnstile site key
# is public by design; the Turnstile secret and demo token secret are synced
# only as Key Vault references, never raw values.
demo_experience_value="${DEMO_EXPERIENCE:-$(get_azd_value DEMO_EXPERIENCE)}"
demo_browser_enabled_value="${DEMO_BROWSER_ENABLED:-$(get_azd_value DEMO_BROWSER_ENABLED)}"
demo_phone_enabled_value="${DEMO_PHONE_ENABLED:-$(get_azd_value DEMO_PHONE_ENABLED)}"
demo_max_seconds_value="${DEMO_MAX_SECONDS:-$(get_azd_value DEMO_MAX_SECONDS)}"
turnstile_site_key_value="${TURNSTILE_SITE_KEY:-$(get_azd_value TURNSTILE_SITE_KEY)}"

if [[ -n "$demo_experience_value" ]]; then
    set_kv "app/demo/experience" "$demo_experience_value" && ((++count)) || errors+=("app/demo/experience")
fi
if [[ -n "$demo_browser_enabled_value" ]]; then
    set_kv "app/demo/browser-enabled" "$demo_browser_enabled_value" && ((++count)) || errors+=("app/demo/browser-enabled")
fi
if [[ -n "$demo_phone_enabled_value" ]]; then
    set_kv "app/demo/phone-enabled" "$demo_phone_enabled_value" && ((++count)) || errors+=("app/demo/phone-enabled")
fi
if [[ -n "$demo_max_seconds_value" ]]; then
    set_kv "app/demo/max-seconds" "$demo_max_seconds_value" && ((++count)) || errors+=("app/demo/max-seconds")
fi
if [[ -n "$turnstile_site_key_value" ]]; then
    set_kv "app/demo/turnstile-site-key" "$turnstile_site_key_value" && ((++count)) || errors+=("app/demo/turnstile-site-key")
fi

if [[ -n "$key_vault_name" ]]; then
    if [[ -n "${TURNSTILE_SECRET_KEY:-}" && "$DRY_RUN" != "true" ]]; then
        az keyvault secret set \
            --vault-name "$key_vault_name" \
            --name turnstile-secret-key \
            --value "$TURNSTILE_SECRET_KEY" \
            --output none >/dev/null
        set_kv_ref "app/demo/turnstile-secret-key" "turnstile-secret-key" && ((++count)) || errors+=("app/demo/turnstile-secret-key")
    elif az keyvault secret show --vault-name "$key_vault_name" --name turnstile-secret-key --query id --output tsv >/dev/null 2>&1; then
        set_kv_ref "app/demo/turnstile-secret-key" "turnstile-secret-key" && ((++count)) || errors+=("app/demo/turnstile-secret-key")
    fi
    if [[ -n "${DEMO_TOKEN_SECRET:-}" && "$DRY_RUN" != "true" ]]; then
        az keyvault secret set \
            --vault-name "$key_vault_name" \
            --name demo-token-secret \
            --value "$DEMO_TOKEN_SECRET" \
            --output none >/dev/null
        set_kv_ref "app/demo/token-secret" "demo-token-secret" && ((++count)) || errors+=("app/demo/token-secret")
    elif az keyvault secret show --vault-name "$key_vault_name" --name demo-token-secret --query id --output tsv >/dev/null 2>&1; then
        set_kv_ref "app/demo/token-secret" "demo-token-secret" && ((++count)) || errors+=("app/demo/token-secret")
    fi
fi

# Redis
set_kv "azure/redis/hostname" "$(get_azd_value REDIS_HOSTNAME)" && ((++count)) || errors+=("azure/redis/hostname")
set_kv "azure/redis/port" "$(get_azd_value REDIS_PORT)" && ((++count)) || errors+=("azure/redis/port")

# Cosmos DB
set_kv "azure/cosmos/database-name" "$(get_azd_value AZURE_COSMOS_DATABASE_NAME)" && ((++count)) || errors+=("azure/cosmos/database-name")
set_kv "azure/cosmos/collection-name" "$(get_azd_value AZURE_COSMOS_COLLECTION_NAME)" && ((++count)) || errors+=("azure/cosmos/collection-name")
# Cosmos Entra connection string (Key Vault reference with OIDC auth for managed identity)
set_kv_ref "azure/cosmos/connection-string" "cosmos-entra-connection-string" && ((++count)) || errors+=("azure/cosmos/connection-string")

# PostgreSQL (Phase 0 Clinic Recall spike)
set_kv "app/postgres/host" "$(get_azd_value POSTGRES_HOST)" && ((++count)) || errors+=("app/postgres/host")
set_kv "app/postgres/database-name" "$(get_azd_value POSTGRES_DATABASE_NAME)" && ((++count)) || errors+=("app/postgres/database-name")
set_kv "app/postgres/admin-login" "$(get_azd_value POSTGRES_ADMIN_LOGIN)" && ((++count)) || errors+=("app/postgres/admin-login")
set_kv_ref "app/postgres/connection-string" "postgres-connection-string" && ((++count)) || errors+=("app/postgres/connection-string")
if [[ -n "$key_vault_name" ]] && az keyvault secret show --vault-name "$key_vault_name" --name clinic-recall-privacy-db-connection-string --query id --output tsv >/dev/null 2>&1; then
    set_kv_ref "app/postgres/privacy-connection-string" "clinic-recall-privacy-db-connection-string" && ((++count)) || errors+=("app/postgres/privacy-connection-string")
fi
audit_db_role_enabled="$(get_azd_value CLINIC_RECALL_AUDIT_DB_ROLE_ENABLED)"
if [[ "$audit_db_role_enabled" == "true" ]]; then
    if az keyvault secret show --vault-name "$key_vault_name" --name clinic-recall-audit-db-connection-string --query id --output tsv >/dev/null 2>&1; then
        if set_kv_ref "app/postgres/audit-connection-string" "clinic-recall-audit-db-connection-string"; then
            ((++count))
        else
            errors+=("app/postgres/audit-connection-string")
            audit_db_configuration_failed=true
        fi
    else
        errors+=("clinic-recall-audit-db-connection-string Key Vault reference is unavailable")
        audit_db_configuration_failed=true
    fi
else
    delete_kv "app/postgres/audit-connection-string" || true
fi

# Storage
set_kv "azure/storage/account-name" "$(get_azd_value AZURE_STORAGE_ACCOUNT_NAME)" && ((++count)) || errors+=("azure/storage/account-name")
set_kv "azure/storage/container-url" "$(get_azd_value AZURE_STORAGE_CONTAINER_URL)" && ((++count)) || errors+=("azure/storage/container-url")

# App Insights
set_kv "azure/appinsights/connection-string" "$(get_azd_value APPLICATIONINSIGHTS_CONNECTION_STRING)" && ((++count)) || errors+=("azure/appinsights/connection-string")

# Voice Live (optional)
set_kv "azure/voicelive/endpoint" "$(get_azd_value AZURE_VOICELIVE_ENDPOINT)" && ((++count)) || errors+=("azure/voicelive/endpoint")
set_kv "azure/voicelive/model" "$(get_azd_value AZURE_VOICELIVE_MODEL)" && ((++count)) || errors+=("azure/voicelive/model")
set_kv "azure/voicelive/resource-id" "$(get_azd_value AZURE_VOICELIVE_RESOURCE_ID)" && ((++count)) || errors+=("azure/voicelive/resource-id")

# AI Foundry (for Evaluations SDK)
# Derive project endpoint from project_id since azapi doesn't expose it directly
# Pattern: https://<account-name>.services.ai.azure.com/api/projects/<project-name>
ai_foundry_project_id=$(get_azd_value ai_foundry_project_id)
if [[ -n "$ai_foundry_project_id" ]]; then
    # Extract account name and project name from resource ID
    # Format: .../accounts/<account-name>/projects/<project-name>
    account_name=$(echo "$ai_foundry_project_id" | sed -n 's|.*/accounts/\([^/]*\)/projects/.*|\1|p')
    project_name=$(echo "$ai_foundry_project_id" | sed -n 's|.*/projects/\([^/]*\)$|\1|p')
    if [[ -n "$account_name" && -n "$project_name" ]]; then
        ai_foundry_project_endpoint="https://${account_name}.services.ai.azure.com/api/projects/${project_name}"
        set_kv "azure/ai-foundry/project-endpoint" "$ai_foundry_project_endpoint" && ((++count)) || errors+=("azure/ai-foundry/project-endpoint")
    fi
fi

# CardAPI MCP server endpoint (self-contained, direct Cosmos DB access)
# Priority: 1. Environment variable override, 2. azd env value, 3. Azure CLI query, 4. Existing App Config value
cardapi_url=""

# Check for environment variable override (from GitHub Actions or local)
if [[ -n "${MCP_SERVER_CARDAPI_URL:-}" ]]; then
    cardapi_url="$MCP_SERVER_CARDAPI_URL"
    info "Using MCP_SERVER_CARDAPI_URL from environment: $cardapi_url"
else
    # Try azd env value (from Terraform outputs)
    cardapi_url=$(get_azd_value CARDAPI_CONTAINER_APP_URL)
    if [[ -n "$cardapi_url" ]]; then
        info "Using CARDAPI_CONTAINER_APP_URL from azd env: $cardapi_url"
    fi
fi

# If still empty, query Azure directly for the Container App FQDN
if [[ -z "$cardapi_url" ]]; then
    resource_group=$(get_azd_value AZURE_RESOURCE_GROUP)
    if [[ -n "$resource_group" ]]; then
        # Find cardapi container app by name pattern
        cardapi_fqdn=$(az containerapp list \
            --resource-group "$resource_group" \
            --query "[?contains(name, 'cardapi')].properties.configuration.ingress.fqdn" \
            --output tsv 2>/dev/null | head -1 | tr -d '\n\r' || echo "")
        if [[ -n "$cardapi_fqdn" ]]; then
            cardapi_url="https://${cardapi_fqdn}"
            info "Discovered CardAPI MCP URL from Azure: $cardapi_url"
        fi
    fi
fi

# If still empty, preserve existing App Config value
if [[ -z "$cardapi_url" ]]; then
    existing_url=$(get_appconfig_value "app/mcp/servers/cardapi/url")
    if [[ -n "$existing_url" ]]; then
        cardapi_url="$existing_url"
        info "Preserving existing app/mcp/servers/cardapi/url: $cardapi_url"
    fi
fi

if [[ -n "$cardapi_url" ]]; then
    # Backend expects this key to load MCP_SERVER_CARDAPI_URL
    set_kv "app/mcp/servers/cardapi/url" "$cardapi_url" && ((++count)) || errors+=("app/mcp/servers/cardapi/url")
else
    warn "CardAPI MCP URL not configured (set MCP_SERVER_CARDAPI_URL or deploy cardapi service)"
fi

# CardAPI MCP auth settings (for EasyAuth-protected deployments)
cardapi_auth_enabled=$(get_azd_value CARDAPI_MCP_AUTH_ENABLED)
cardapi_app_id=$(get_azd_value CARDAPI_MCP_APP_ID)
if [[ -n "$cardapi_auth_enabled" ]]; then
    set_kv "app/mcp/servers/cardapi/auth-enabled" "$cardapi_auth_enabled" && ((++count)) || errors+=("app/mcp/servers/cardapi/auth-enabled")
fi
if [[ -n "$cardapi_app_id" ]]; then
    set_kv "app/mcp/servers/cardapi/app-id" "$cardapi_app_id" && ((++count)) || errors+=("app/mcp/servers/cardapi/app-id")
fi

# Environment metadata
set_kv "app/environment" "$(get_azd_value AZURE_ENV_NAME)" && ((++count)) || errors+=("app/environment")

# Sentinel for refresh trigger
set_kv "app/sentinel" "v$(date +%s)" && ((++count)) || errors+=("app/sentinel")

echo "├─────────────────────────────────────────────────────────────"
if [[ ${#errors[@]} -gt 0 ]]; then
    warn "Sync completed with ${#errors[@]} errors ($count keys synced)"
    log "  Failed keys:"
    for error in "${errors[@]}"; do
        log "    • $error"
    done
else
    success "Sync complete: $count infrastructure keys"
fi
echo "╰─────────────────────────────────────────────────────────────"
echo ""
if [[ "$cliniko_configuration_failed" == "true" || "$audit_db_configuration_failed" == "true" ]]; then
    exit 1
fi
