#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash devops/agentops/run-recall-agent-gate.sh [options]

Repeatable hosted agent governance gate runner. By default it pins azd to the
same hosted agent version as src/recall-agent/eval.yaml, keeps gate-only dependencies in
.venv-agentops, and exports both Azure token names needed by AgentOps/LiteLLM.

Options:
  --env NAME              azd environment to select (default: phase0)
    --agent-dir DIR         hosted agent directory (default: src/recall-agent)
    --config PATH           AgentOps config file (default: agentops.yaml)
  --version VERSION       hosted agent version to pin (default: eval.yaml agent.version)
  --eval-only             run only agentops eval
  --skip-assert           skip ASSERT
  --skip-redteam          skip Red Team
  --skip-doctor           skip Doctor evidence pack
  --doctor-nonblocking    write Doctor evidence but do not fail on Doctor exit code
    --doctor-exclude-rules RULES
                                                 comma-separated Doctor posture rule ids to exclude
  --no-build-env          do not auto-build .venv-agentops if missing
  -h, --help              show this help
EOF
}

log() {
    printf '\n==> %s\n' "$*"
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
azd_env="${AZURE_ENV_NAME:-phase0}"
agent_dir="src/recall-agent"
config_path="agentops.yaml"
requested_agent_version=""
run_assert=1
run_redteam=1
run_doctor=1
doctor_nonblocking=0
doctor_exclude_rules=""
build_env=1

while (($#)); do
    case "$1" in
        --env)
            [[ $# -ge 2 ]] || fail "--env requires a value"
            azd_env="$2"
            shift 2
            ;;
        --agent-dir)
            [[ $# -ge 2 ]] || fail "--agent-dir requires a value"
            agent_dir="${2%/}"
            shift 2
            ;;
        --config)
            [[ $# -ge 2 ]] || fail "--config requires a value"
            config_path="$2"
            shift 2
            ;;
        --version)
            [[ $# -ge 2 ]] || fail "--version requires a value"
            requested_agent_version="$2"
            shift 2
            ;;
        --eval-only)
            run_assert=0
            run_redteam=0
            run_doctor=0
            shift
            ;;
        --skip-assert)
            run_assert=0
            shift
            ;;
        --skip-redteam)
            run_redteam=0
            shift
            ;;
        --skip-doctor)
            run_doctor=0
            shift
            ;;
        --doctor-nonblocking)
            doctor_nonblocking=1
            shift
            ;;
        --doctor-exclude-rules)
            [[ $# -ge 2 ]] || fail "--doctor-exclude-rules requires a value"
            doctor_exclude_rules="$2"
            shift 2
            ;;
        --no-build-env)
            build_env=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

cd "$repo_root"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/uv-cache}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

eval_path="$agent_dir/eval.yaml"
requirements_path="$agent_dir/requirements.txt"
[[ -f "$eval_path" ]] || fail "missing eval recipe: $eval_path"
[[ -f "$requirements_path" ]] || fail "missing hosted requirements: $requirements_path"
[[ -f "$config_path" ]] || fail "missing AgentOps config: $config_path"
eval_backup="$(mktemp)"
cp "$eval_path" "$eval_backup"
azure_yaml_backup="$(mktemp)"
cp azure.yaml "$azure_yaml_backup"

restore_eval_recipe() {
    if [[ -n "${eval_backup:-}" && -f "$eval_backup" ]]; then
        cp "$eval_backup" "$eval_path"
        rm -f "$eval_backup"
    fi
    if [[ -n "${azure_yaml_backup:-}" && -f "$azure_yaml_backup" ]]; then
        cp "$azure_yaml_backup" azure.yaml
        rm -f "$azure_yaml_backup"
    fi
}
trap restore_eval_recipe EXIT

require_command az
require_command azd
require_command uv

log "Hydrating/pinning AgentOps gate environment aliases"
# shellcheck source=/dev/null
source <("$repo_root/devops/agentops/hydrate-gate-env.sh" --env "$azd_env" --export)

agent_version="$requested_agent_version"
if [[ -z "$agent_version" ]]; then
    agent_version="$(awk '
        /^agent:/ { in_agent=1; next }
        in_agent && /^[^[:space:]]/ { in_agent=0 }
        in_agent && /^[[:space:]]*version:/ {
            value=$0
            sub(/^[[:space:]]*version:[[:space:]]*/, "", value)
            gsub(/["[:space:]]/, "", value)
            print value
            exit
        }
    ' "$eval_path")"
fi
[[ -n "$agent_version" ]] || fail "could not resolve agent.version from $eval_path"

agent_name="$(awk '
    /^agent:/ { in_agent=1; next }
    in_agent && /^[^[:space:]]/ { in_agent=0 }
    in_agent && /^[[:space:]]*name:/ {
        value=$0
        sub(/^[[:space:]]*name:[[:space:]]*/, "", value)
        gsub(/["[:space:]]/, "", value)
        print value
        exit
    }
' "$eval_path")"
[[ -n "$agent_name" ]] || fail "could not resolve agent.name from $eval_path"

agentops_agent="$(awk '/^agent:/ { sub(/^agent:[[:space:]]*/, ""); gsub(/"/, ""); print; exit }' "$config_path")"
[[ -n "$agentops_agent" ]] || fail "could not resolve agent from $config_path"
agentops_version="${agentops_agent##*:}"
expected_agent="$agent_name:$agent_version"
if [[ "$agentops_agent" != "$expected_agent" || "$agentops_version" != "$agent_version" ]]; then
    fail "version drift: $config_path targets $agentops_agent but $eval_path targets $expected_agent"
fi

grep -qx 'mcp' "$requirements_path" \
    || fail "$requirements_path must include mcp for hosted Foundry runtime startup"

agent_env_key="$(printf '%s' "$agent_name" | tr '[:lower:]-' '[:upper:]_')"
agent_name_var="AGENT_${agent_env_key}_NAME"
agent_version_var="AGENT_${agent_env_key}_VERSION"

log "Selecting azd env $azd_env and pinning $expected_agent"
azd env select "$azd_env" >/dev/null
azd env set "$agent_name_var" "$agent_name" >/dev/null
azd env set "$agent_version_var" "$agent_version" >/dev/null
# The azd azure.ai.agents extension selects the eval/generation target from the
# GENERIC keys AGENT_NAME/AGENT_VERSION (not the per-agent scoped aliases).
# With two agent services in azure.yaml, leaving these unset makes generation
# silently run against the wrong agent (seen 2026-07-03: recall recipe graded
# inbound-assistant:2 responses). Pin them for this gate run.
azd env set AGENT_NAME "$agent_name" >/dev/null
azd env set AGENT_VERSION "$agent_version" >/dev/null
current_name="$(azd env get-values | awk -F= -v key="$agent_name_var" '$1 == key { gsub(/"/, "", $2); print $2; exit }')"
current_version="$(azd env get-values | awk -F= -v key="$agent_version_var" '$1 == key { gsub(/"/, "", $2); print $2; exit }')"
[[ "$current_name" == "$agent_name" ]] \
    || fail "azd env $agent_name_var=$current_name, expected $agent_name"
[[ "$current_version" == "$agent_version" ]] \
    || fail "azd env $agent_version_var=$current_version, expected $agent_version"

if (( run_assert || run_redteam || run_doctor )); then
    if [[ ! -x .venv-agentops/bin/agentops || ! -x .venv-agentops/bin/assert-ai ]]; then
        if (( ! build_env )); then
            fail ".venv-agentops is missing; run make agentops_gate_env or omit --no-build-env"
        fi
        log "Building isolated AgentOps gate environment"
        make agentops_gate_env
    fi
fi

log "Running Foundry eval gate"
# The azd azure.ai.agents extension caches the Foundry eval object AND the
# dataset/response generation operations in shared azd env keys (LAST_EVAL_ID,
# LAST_EVAL_GEN_OP_ID, LAST_EVAL_DATASET_GEN_OP_ID, *_STATUS). Without scoping,
# gates for different agents silently reuse each other's eval object and stale
# generated responses, grading the WRONG agent (seen 2026-07-03: recall recipe
# rows answered by inbound-assistant:2, plus empty stale outputs). A release
# gate must regenerate everything against the pinned agent: clear the cache.
last_eval_keys=(
    LAST_EVAL_ID
    LAST_EVAL_INIT_STATUS
    LAST_EVAL_GEN_OP_ID
    LAST_EVAL_GEN_STATUS
    LAST_EVAL_DATASET_GEN_OP_ID
    LAST_EVAL_DATASET_GEN_STATUS
)
log "Clearing cached azd eval state so responses regenerate against $expected_agent"
for key in "${last_eval_keys[@]}"; do
    azd env set "$key" "" >/dev/null
done
(
    cd "$agent_dir"
    # ROOT CAUSE (verified in azure-dev source, extensions/azure.ai.agents
    # internal/cmd/eval.go + eval_run.go): `azd ai agent eval run` has NO
    # --agent flag. resolveEvalContext -> resolveAgentService auto-selects the
    # FIRST azure.ai.agent service in azure.yaml when several exist (no-prompt
    # mode), then overrides the recipe's agent with AGENT_<FIRSTSVC>_NAME/
    # _VERSION from the azd env and even resolves the --config path against
    # that service's project dir. With two agents, the recall gate silently
    # graded inbound-assistant. Deterministic fix: temporarily scope
    # azure.yaml to THIS gate's agent service for the eval run (restored by
    # the EXIT trap; also restored immediately below).
    python3 - "$repo_root/azure.yaml" "$agent_name" <<'PY'
import re, sys
path, keep = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines(keepends=True)
out = []
i = 0
in_services = False
while i < len(lines):
    line = lines[i]
    if re.match(r'^services:\s*$', line):
        in_services = True
        out.append(line)
        i += 1
        continue
    if in_services and re.match(r'^\S', line):
        in_services = False
    m = re.match(r'^    ([A-Za-z0-9_-]+):\s*$', line) if in_services else None
    if m:
        j = i + 1
        while j < len(lines) and not re.match(r'^    [A-Za-z0-9_-]+:\s*$', lines[j]) and not re.match(r'^\S', lines[j]):
            j += 1
        block = ''.join(lines[i:j])
        if 'host: azure.ai.agent' in block and m.group(1) != keep:
            i = j
            continue
        out.append(block)
        i = j
        continue
    out.append(line)
    i += 1
open(path, 'w').write(''.join(out))
PY
    AGENT_NAME="$agent_name" AGENT_VERSION="$agent_version" \
        uv run agentops eval run --config "$repo_root/$config_path"
    status=$?
    cp "$azure_yaml_backup" "$repo_root/azure.yaml"
    exit "$status"
)
new_eval_id="$(azd env get-values | awk -F= '$1 == "LAST_EVAL_ID" { gsub(/"/, "", $2); print $2; exit }')"
if [[ -n "$new_eval_id" ]]; then
    agent_eval_id_var="AGENT_${agent_env_key}_EVAL_ID"
    log "Recording $agent_name eval object id $new_eval_id in $agent_eval_id_var"
    azd env set "$agent_eval_id_var" "$new_eval_id" >/dev/null
fi

if (( run_assert )); then
    log "Running ASSERT gate"
    token="$(az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv)"
    PATH="$repo_root/.venv-agentops/bin:$PATH" \
        AZURE_AD_TOKEN="$token" \
        AZURE_OPENAI_AD_TOKEN="$token" \
        .venv-agentops/bin/agentops assert run --config "$config_path"
fi

if (( run_redteam )); then
    log "Running Red Team gate"
    PATH="$repo_root/.venv-agentops/bin:$PATH" \
        OPENAI_CHAT_ENDPOINT="${OPENAI_CHAT_ENDPOINT:-}" \
        OPENAI_CHAT_MODEL="${OPENAI_CHAT_MODEL:-}" \
        OPENAI_CHAT_KEY="${OPENAI_CHAT_KEY:-}" \
        .venv-agentops/bin/agentops redteam run --config "$config_path"
fi

if (( run_doctor )); then
    log "Running Doctor evidence pack"
    doctor_args=(--evidence-pack)
    if [[ -n "$doctor_exclude_rules" ]]; then
        doctor_args+=(--exclude-rules "$doctor_exclude_rules")
    fi
    set +e
    PATH="$repo_root/.venv-agentops/bin:$PATH" \
        AGENTOPS_APPLICATIONINSIGHTS_CONNECTION_STRING="${AGENTOPS_APPLICATIONINSIGHTS_CONNECTION_STRING:-}" \
        APPLICATIONINSIGHTS_CONNECTION_STRING= \
        .venv-agentops/bin/agentops doctor "${doctor_args[@]}"
    doctor_status=$?
    set -e
    if (( doctor_status != 0 )); then
        if (( doctor_nonblocking )); then
            printf 'WARNING: Doctor exited %s; evidence was still written.\n' "$doctor_status" >&2
        else
            exit "$doctor_status"
        fi
    fi
fi

log "$agent_name gate run complete"