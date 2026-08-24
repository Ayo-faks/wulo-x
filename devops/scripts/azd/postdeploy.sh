#!/bin/bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${BLUE+x}" ]]; then BLUE=$'\033[0;34m'; fi
if [[ -z "${GREEN+x}" ]]; then GREEN=$'\033[0;32m'; fi
if [[ -z "${YELLOW+x}" ]]; then YELLOW=$'\033[1;33m'; fi
if [[ -z "${RED+x}" ]]; then RED=$'\033[0;31m'; fi
if [[ -z "${DIM+x}" ]]; then DIM=$'\033[2m'; fi
if [[ -z "${NC+x}" ]]; then NC=$'\033[0m'; fi
readonly BLUE GREEN YELLOW RED DIM NC

log() { printf '│ %s%s%s\n' "$DIM" "$*" "$NC"; }
info() { printf '│ %s%s%s\n' "$BLUE" "$*" "$NC"; }
success() { printf '│ %s✔%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '│ %s⚠%s  %s\n' "$YELLOW" "$NC" "$*"; }
fail() { printf '│ %s✖%s %s\n' "$RED" "$NC" "$*" >&2; }

azd_get() {
    # azd (>=1.25) can print "ERROR: key not found ..." to stdout (sometimes with a
    # leading blank line) instead of failing silently, so sanitise the output:
    # keep the first non-empty line and drop anything that looks like an error.
    local value
    value=$(azd env get-value "$1" 2>/dev/null | grep -m1 -v '^[[:space:]]*$' || true)
    if [[ -z "$value" || "$value" == ERROR* || "$value" == *"key not found"* ]]; then
        echo ""
        return 0
    fi
    echo "$value"
}

task_migrate_clinic_recall() {
    local resource_group backend_app postgres_host postgres_database authorization command
    resource_group="$(azd_get AZURE_RESOURCE_GROUP)"
    backend_app="$(azd_get BACKEND_CONTAINER_APP_NAME)"
    postgres_host="$(azd_get POSTGRES_HOST)"
    postgres_database="$(azd_get POSTGRES_DATABASE_NAME)"
    authorization="$(azd_get CLINIC_RECALL_MIGRATION_AUTHORIZATION)"

    if [[ -z "$resource_group" || -z "$backend_app" || -z "$postgres_host" || -z "$postgres_database" ]]; then
        fail "Backend and PostgreSQL target identity are required for migration"
        return 1
    fi
    if [[ ! "$postgres_host" =~ ^[A-Za-z0-9.-]+$ || ! "$postgres_database" =~ ^[A-Za-z0-9_-]+$ ]]; then
        fail "PostgreSQL target identity is invalid"
        return 1
    fi
    if [[ -n "$authorization" && ! "$authorization" =~ ^[0-9a-f]{40}:[A-Za-z0-9_]+$ ]]; then
        fail "Migration authorization must be an exact source SHA and Alembic head"
        return 1
    fi

    command="python -m src.clinic_recall.release_migration --expected-host '$postgres_host' --expected-database '$postgres_database'"
    if [[ -n "$authorization" ]]; then
        command="$command --authorization '$authorization'"
    fi
    info "Verifying Clinic Recall database migration head"
    az containerapp exec \
        --resource-group "$resource_group" \
        --name "$backend_app" \
        --command "$command" \
        --output none
}

task_configure_clinic_recall_audit_role() {
    local resource_group backend_app postgres_host postgres_database enabled authorization command
    resource_group="$(azd_get AZURE_RESOURCE_GROUP)"
    backend_app="$(azd_get BACKEND_CONTAINER_APP_NAME)"
    postgres_host="$(azd_get POSTGRES_HOST)"
    postgres_database="$(azd_get POSTGRES_DATABASE_NAME)"
    enabled="$(azd_get CLINIC_RECALL_AUDIT_DB_ROLE_ENABLED)"
    authorization="$(azd_get CLINIC_RECALL_AUDIT_ROLE_AUTHORIZATION)"

    if [[ "$enabled" != "true" ]]; then
        info "Clinic Recall release inventory role is disabled"
        return 0
    fi
    if [[ -z "$resource_group" || -z "$backend_app" || -z "$postgres_host" || -z "$postgres_database" || -z "$authorization" ]]; then
        fail "Exact audit-role authorization and database target identity are required"
        return 1
    fi
    if [[ ! "$postgres_host" =~ ^[A-Za-z0-9.-]+$ || ! "$postgres_database" =~ ^[A-Za-z0-9_-]+$ ]]; then
        fail "PostgreSQL target identity is invalid"
        return 1
    fi
    if [[ ! "$authorization" =~ ^[0-9a-f]{40}:[A-Za-z0-9_]+$ ]]; then
        fail "Audit-role authorization must be an exact source SHA and Alembic head"
        return 1
    fi

    command="python -m src.clinic_recall.release_audit --expected-host '$postgres_host' --expected-database '$postgres_database' --authorization '$authorization'"
    info "Configuring ordinary Clinic Recall release inventory role"
    az containerapp exec \
        --resource-group "$resource_group" \
        --name "$backend_app" \
        --command "$command" \
        --output none
}

# Event Grid validates webhooks with a POST from Azure datacenter IPs. If the
# endpoint sits behind Cloudflare, a WAF/bot rule that blocks datacenter IPs
# will fail the handshake with a connection-level error even when browser/curl
# traffic succeeds. Probe first so the failure mode is obvious in deploy logs.
preflight_endpoint() {
    local url="$1"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$url" \
        -H 'Content-Type: application/json' \
        -H 'aeg-event-type: SubscriptionValidation' \
        -d '[{"eventType":"Microsoft.EventGrid.SubscriptionValidationEvent","data":{"validationCode":"preflight"},"dataVersion":"1"}]' || echo 000)
    if [[ "$code" != "200" ]]; then
        fail "Webhook preflight to $url returned HTTP $code (expected 200)."
        fail "Event Grid validation will likely fail. If this endpoint is behind Cloudflare, add a WAF skip rule for Azure Event Grid source IPs."
        return 1
    fi
    log "Webhook preflight OK: $url"
}

assert_subscription_succeeded() {
    local name="$1" resource_group="$2" topic="$3"
    local state attempt
    for attempt in 1 2 3 4 5 6; do
        state=$(az eventgrid system-topic event-subscription show \
            --resource-group "$resource_group" \
            --system-topic-name "$topic" \
            --name "$name" \
            --query provisioningState --output tsv 2>/dev/null || echo Missing)
        [[ "$state" == "Succeeded" ]] && { success "Subscription $name provisioningState=Succeeded"; return 0; }
        [[ "$state" == "Failed" ]] && break
        sleep 10
    done
    fail "Subscription $name provisioningState=$state (expected Succeeded)"
    return 1
}

upsert_event_subscription() {
    local name="$1" endpoint="$2" event_type="$3" resource_group="$4" topic="$5"

    if az eventgrid system-topic event-subscription show \
        --resource-group "$resource_group" \
        --system-topic-name "$topic" \
        --name "$name" \
        --output none >/dev/null 2>&1; then
        log "Updating Event Grid subscription $name -> $endpoint"
        if ! az eventgrid system-topic event-subscription update \
            --resource-group "$resource_group" \
            --system-topic-name "$topic" \
            --name "$name" \
            --endpoint "$endpoint" \
            --endpoint-type webhook \
            --included-event-types "$event_type" \
            --output none; then
            fail "Event Grid subscription $name could not be updated"
            return 1
        fi
    else
        log "Creating Event Grid subscription $name -> $endpoint"
        if ! az eventgrid system-topic event-subscription create \
            --resource-group "$resource_group" \
            --system-topic-name "$topic" \
            --name "$name" \
            --endpoint "$endpoint" \
            --endpoint-type webhook \
            --included-event-types "$event_type" \
            --max-delivery-attempts 5 \
            --event-ttl 1440 \
            --output none; then
            fail "Event Grid subscription $name could not be created"
            return 1
        fi
    fi

    assert_subscription_succeeded "$name" "$resource_group" "$topic"
}

main() {
    echo ""
    echo "╭─────────────────────────────────────────────────────────────"
    echo "│ ${BLUE}🚀 Post-Deploying Event Grid Webhooks${NC}"
    echo "├─────────────────────────────────────────────────────────────"

    task_migrate_clinic_recall || exit 1
    task_configure_clinic_recall_audit_role || exit 1

    local resource_group topic backend_url public_hostname
    resource_group="$(azd_get AZURE_RESOURCE_GROUP)"
    topic="$(azd_get ACS_EVENTGRID_SYSTEM_TOPIC_NAME)"
    public_hostname="$(azd_get APPLICATION_GATEWAY_HOSTNAME)"
    if [[ -n "$public_hostname" && "$public_hostname" != ERROR* ]]; then
        backend_url="https://${public_hostname}"
    else
        backend_url="$(azd_get BACKEND_CONTAINER_APP_URL)"
    fi

    if [[ -z "$backend_url" || "$backend_url" == ERROR* ]]; then
        local backend_fqdn
        backend_fqdn="$(azd_get BACKEND_CONTAINER_APP_FQDN)"
        [[ -n "$backend_fqdn" && "$backend_fqdn" != ERROR* ]] && backend_url="https://${backend_fqdn}"
    fi

    if [[ -z "$resource_group" || "$resource_group" == ERROR* || -z "$topic" || "$topic" == ERROR* || -z "$backend_url" || "$backend_url" == ERROR* ]]; then
        fail "Missing resource group, ACS Event Grid topic, or backend URL; cannot configure inbound call/SMS webhooks"
        echo "╰─────────────────────────────────────────────────────────────"
        exit 1
    fi

    info "Backend: $backend_url"
    info "ACS Event Grid topic: $topic"

    preflight_endpoint "${backend_url}/api/v1/calls/answer" || exit 1

    upsert_event_subscription \
        "backend-incoming-call-handler" \
        "${backend_url}/api/v1/calls/answer" \
        "Microsoft.Communication.IncomingCall" \
        "$resource_group" \
        "$topic" || exit 1

    upsert_event_subscription \
        "backend-sms-events-handler" \
        "${backend_url}/api/v1/sms/events" \
        "Microsoft.Communication.SMSReceived" \
        "$resource_group" \
        "$topic" || exit 1

    success "Event Grid webhooks configured"
    echo "╰─────────────────────────────────────────────────────────────"
    echo ""
}

main "$@"