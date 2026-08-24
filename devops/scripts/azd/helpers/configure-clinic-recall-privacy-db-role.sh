#!/bin/bash
set -euo pipefail

readonly ROLE_NAME="clinic_recall_privacy"
FIREWALL_RULE=""
CLEANUP_RESOURCE_GROUP=""
CLEANUP_SERVER_NAME=""

log() { printf '│ %s\n' "$*"; }
warn() { printf '│ ⚠  %s\n' "$*" >&2; }
fail() { printf '│ ✖  %s\n' "$*" >&2; }

azd_get() {
    local value
    value="$(azd env get-value "$1" 2>/dev/null || echo "")"
    if [[ "$value" == "null" || "$value" == ERROR* ]]; then
        value=""
    fi
    printf '%s' "$value"
}

require_value() {
    local name="$1" value="$2"
    if [[ -z "$value" || "$value" == "null" || "$value" == ERROR* ]]; then
        fail "$name is required to configure the Clinic Recall privacy database role"
        exit 1
    fi
}

cleanup() {
    unset PGPASSWORD PRIVACY_PASSWORD PRIVACY_DATABASE_NAME
    if [[ -n "$FIREWALL_RULE" ]]; then
        az postgres flexible-server firewall-rule delete \
            --resource-group "$CLEANUP_RESOURCE_GROUP" \
            --name "$CLEANUP_SERVER_NAME" \
            --rule-name "$FIREWALL_RULE" \
            --yes \
            --output none >/dev/null 2>&1 || true
    fi
}

main() {
    local rights_job retention_job
    rights_job="$(azd_get CLINIC_RECALL_RIGHTS_JOB_NAME)"
    retention_job="$(azd_get CLINIC_RECALL_RETENTION_JOB_NAME)"
    if [[ -z "$rights_job" && -z "$retention_job" ]]; then
        log "Privacy Jobs are disabled; ordinary PostgreSQL role setup is not required"
        return 0
    fi

    for command_name in az azd psql; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            fail "$command_name is required while a privacy Job resource exists"
            return 1
        fi
    done

    local resource_group server_name host database admin_login key_vault env_name rotation_epoch
    local admin_password privacy_password public_ip sslmode="require"
    resource_group="$(azd_get AZURE_RESOURCE_GROUP)"
    server_name="$(azd_get POSTGRES_SERVER_NAME)"
    host="$(azd_get POSTGRES_HOST)"
    database="$(azd_get POSTGRES_DATABASE_NAME)"
    admin_login="$(azd_get POSTGRES_ADMIN_LOGIN)"
    key_vault="$(azd_get AZURE_KEY_VAULT_NAME)"
    env_name="$(azd_get AZURE_ENV_NAME)"
    rotation_epoch="$(azd_get CLINIC_RECALL_PRIVACY_DB_PASSWORD_ROTATION_EPOCH)"

    require_value AZURE_RESOURCE_GROUP "$resource_group"
    require_value POSTGRES_SERVER_NAME "$server_name"
    require_value POSTGRES_HOST "$host"
    require_value POSTGRES_DATABASE_NAME "$database"
    require_value POSTGRES_ADMIN_LOGIN "$admin_login"
    require_value AZURE_KEY_VAULT_NAME "$key_vault"
    require_value CLINIC_RECALL_PRIVACY_DB_PASSWORD_ROTATION_EPOCH "$rotation_epoch"

    if [[ "${ROLE_HELPER_ALLOW_INSECURE_LOCAL_TEST:-false}" == "true" ]]; then
        if [[ "$host" != "127.0.0.1" && "$host" != "localhost" ]]; then
            fail "Insecure role-helper test mode is restricted to localhost"
            return 1
        fi
        sslmode="disable"
    fi

    admin_password="$(az keyvault secret show \
        --vault-name "$key_vault" \
        --name postgres-admin-password \
        --query value \
        --output tsv 2>/dev/null || echo "")"
    privacy_password="$(az keyvault secret show \
        --vault-name "$key_vault" \
        --name clinic-recall-privacy-db-password \
        --query value \
        --output tsv 2>/dev/null || echo "")"
    require_value postgres-admin-password "$admin_password"
    require_value clinic-recall-privacy-db-password "$privacy_password"

    CLEANUP_RESOURCE_GROUP="$resource_group"
    CLEANUP_SERVER_NAME="$server_name"
    trap cleanup EXIT

    if command -v curl >/dev/null 2>&1; then
        public_ip="$(curl -fsS https://api.ipify.org 2>/dev/null || echo "")"
        if [[ "$public_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            FIREWALL_RULE="privacy-role-${env_name:-dev}-$$"
            az postgres flexible-server firewall-rule create \
                --resource-group "$resource_group" \
                --name "$server_name" \
                --rule-name "$FIREWALL_RULE" \
                --start-ip-address "$public_ip" \
                --end-ip-address "$public_ip" \
                --output none >/dev/null
        else
            warn "Could not resolve the runner IP; attempting the configured database route"
        fi
    fi

    export PRIVACY_PASSWORD="$privacy_password"
    export PRIVACY_DATABASE_NAME="$database"
    export PGPASSWORD="$admin_password"
    psql \
        "host=$host port=5432 dbname=$database user=$admin_login sslmode=$sslmode" \
        -X \
        -v ON_ERROR_STOP=1 \
        >/dev/null <<'SQL'
\getenv privacy_password PRIVACY_PASSWORD
\getenv database_name PRIVACY_DATABASE_NAME

SELECT format(
    'CREATE ROLE clinic_recall_privacy LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT',
    :'privacy_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clinic_recall_privacy')
\gexec

SELECT format(
    'ALTER ROLE clinic_recall_privacy LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT',
    :'privacy_password'
)
\gexec

GRANT CONNECT ON DATABASE :"database_name" TO clinic_recall_privacy;
GRANT USAGE ON SCHEMA public TO clinic_recall_privacy;
GRANT SELECT ON clinic TO clinic_recall_privacy;
GRANT SELECT, DELETE ON patient, appointment, outreach_job, interaction,
    booking_action, escalation, inbound_call, inbound_message,
    inbound_staff_task, call_record TO clinic_recall_privacy;
GRANT UPDATE (id) ON patient, outreach_job TO clinic_recall_privacy;
GRANT UPDATE (content) ON interaction TO clinic_recall_privacy;
GRANT UPDATE (recording_status, recording_stop_requested_at)
    ON call_record TO clinic_recall_privacy;
GRANT SELECT, UPDATE ON pilot_participant, availability_slot, incident_report,
    provider_callback_receipt TO clinic_recall_privacy;
GRANT SELECT, INSERT, UPDATE, DELETE ON rights_request, rights_target,
    external_effect TO clinic_recall_privacy;
GRANT SELECT, INSERT ON external_effect_handoff, audit_log TO clinic_recall_privacy;
REVOKE ALL ON clinic_identity_mapping, clinic_phone_number FROM clinic_recall_privacy;
SQL
    unset PGPASSWORD

    local role_evidence
    role_evidence="$(PGPASSWORD="$privacy_password" psql \
        "host=$host port=5432 dbname=$database user=$ROLE_NAME sslmode=$sslmode" \
        -X -A -t -F '|' \
        -v ON_ERROR_STOP=1 \
        -c "SELECT current_user, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user" \
        2>/dev/null)"
    if [[ "$role_evidence" != "${ROLE_NAME}|f|f|f|f" ]]; then
        fail "Privacy database role verification failed"
        return 1
    fi

    azd env set TF_VAR_clinic_recall_privacy_db_role_ready true >/dev/null
    azd env set TF_VAR_clinic_recall_privacy_db_role_ready_epoch "$rotation_epoch" >/dev/null
    log "Verified ordinary PostgreSQL privacy role (NOSUPERUSER, NOBYPASSRLS)"
}

main "$@"