#!/bin/bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
readonly SEED_SQL="$REPO_ROOT/infra/postgres/phase0_missed_appointments.sql"

log() { printf '│ %s\n' "$*"; }
warn() { printf '│ ⚠  %s\n' "$*"; }

azd_get() {
    azd env get-value "$1" 2>/dev/null || echo ""
}

require_value() {
    local name="$1" value="$2"
    if [[ -z "$value" || "$value" == ERROR* ]]; then
        warn "$name is not set; skipping Postgres seed"
        exit 0
    fi
}

main() {
    if [[ ! -f "$SEED_SQL" ]]; then
        warn "Seed SQL not found: $SEED_SQL"
        exit 0
    fi
    if ! command -v psql >/dev/null 2>&1; then
        warn "psql is not installed; skipping Postgres seed"
        exit 0
    fi

    local resource_group server_name host database admin_login key_vault password public_ip env_name
    resource_group="$(azd_get AZURE_RESOURCE_GROUP)"
    server_name="$(azd_get POSTGRES_SERVER_NAME)"
    host="$(azd_get POSTGRES_HOST)"
    database="$(azd_get POSTGRES_DATABASE_NAME)"
    admin_login="$(azd_get POSTGRES_ADMIN_LOGIN)"
    key_vault="$(azd_get AZURE_KEY_VAULT_NAME)"
    env_name="$(azd_get AZURE_ENV_NAME)"

    require_value AZURE_RESOURCE_GROUP "$resource_group"
    require_value POSTGRES_SERVER_NAME "$server_name"
    require_value POSTGRES_HOST "$host"
    require_value POSTGRES_DATABASE_NAME "$database"
    require_value POSTGRES_ADMIN_LOGIN "$admin_login"
    require_value AZURE_KEY_VAULT_NAME "$key_vault"

    password="$(az keyvault secret show --vault-name "$key_vault" --name postgres-admin-password --query value -o tsv 2>/dev/null || echo "")"
    require_value postgres-admin-password "$password"

    if command -v curl >/dev/null 2>&1; then
        public_ip="$(curl -fsS https://api.ipify.org 2>/dev/null || echo "")"
        if [[ "$public_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            log "Opening Postgres firewall for current seed runner IP"
            az postgres flexible-server firewall-rule create \
                --resource-group "$resource_group" \
                --name "$server_name" \
                --rule-name "phase0-seed-${env_name:-dev}" \
                --start-ip-address "$public_ip" \
                --end-ip-address "$public_ip" \
                --output none >/dev/null
        else
            warn "Could not resolve public IP; psql may be blocked by firewall"
        fi
    fi

    log "Seeding Phase 0 missed appointments into $database on $host"
    PGPASSWORD="$password" psql \
        "host=$host port=5432 dbname=$database user=$admin_login sslmode=require" \
        -v ON_ERROR_STOP=1 \
        -f "$SEED_SQL"
    unset PGPASSWORD password
}

main "$@"