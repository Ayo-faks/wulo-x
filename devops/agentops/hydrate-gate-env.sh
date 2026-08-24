#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash devops/agentops/hydrate-gate-env.sh [options]

Pin local AgentOps/ASSERT/Red Team environment aliases from .env/.env.local and
azd env, without printing secret values. This fixes the recurring drift where
Foundry endpoint and PyRIT OPENAI_CHAT_* aliases disappear between sessions.

Options:
  --env NAME          azd environment to read/select (default: AZURE_ENV_NAME or phase0)
  --env-file PATH     env file to update (default: .env)
  --export            print shell export statements for sourcing (values are not logged if sourced)
  --check             print SET/missing status only
  --no-write          do not update the env file
  --no-azd-set        do not write non-secret Foundry aliases back to azd env
  -h, --help          show this help
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
azd_env="${AZURE_ENV_NAME:-phase0}"
env_file="$repo_root/.env"
print_exports=0
check_only=0
write_env=1
write_azd=1

while (($#)); do
    case "$1" in
        --env)
            [[ $# -ge 2 ]] || { echo "ERROR: --env requires a value" >&2; exit 1; }
            azd_env="$2"
            shift 2
            ;;
        --env-file)
            [[ $# -ge 2 ]] || { echo "ERROR: --env-file requires a value" >&2; exit 1; }
            env_file="$2"
            shift 2
            ;;
        --export)
            print_exports=1
            shift
            ;;
        --check)
            check_only=1
            shift
            ;;
        --no-write)
            write_env=0
            shift
            ;;
        --no-azd-set)
            write_azd=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown option: $1" >&2
            exit 1
            ;;
    esac
done

cd "$repo_root"
mkdir -p .tmp
azd_values_file="$(mktemp .tmp/azd-env-values.XXXXXX)"
trap 'rm -f "$azd_values_file"' EXIT

if command -v azd >/dev/null 2>&1; then
    azd env select "$azd_env" >/dev/null 2>&1 || true
    azd env get-values >"$azd_values_file" 2>/dev/null || true
fi

python - "$repo_root" "$env_file" "$azd_values_file" "$print_exports" "$check_only" "$write_env" "$write_azd" <<'PY'
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
env_file = Path(sys.argv[2])
azd_values_file = Path(sys.argv[3])
print_exports = sys.argv[4] == "1"
check_only = sys.argv[5] == "1"
write_env = sys.argv[6] == "1"
write_azd = sys.argv[7] == "1"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
            raw = raw[1:-1]
        values[key] = raw
    return values


def parse_azd_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        raw = raw.strip()
        if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
            raw = raw[1:-1]
        values[key.strip()] = raw
    return values


def eval_agent_version() -> str:
    path = repo_root / "src/recall-agent/eval.yaml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    in_agent = False
    for line in text.splitlines():
        if line.startswith("agent:"):
            in_agent = True
            continue
        if in_agent and line and not line.startswith((" ", "\t")):
            in_agent = False
        if in_agent and "version:" in line:
            return line.split("version:", 1)[1].strip().strip('"\'')
    return ""


base_env = parse_env_file(repo_root / ".env")
local_env = parse_env_file(repo_root / ".env.local")
azd_env = parse_azd_values(azd_values_file)
shell_env = {key: value for key, value in os.environ.items() if value}


def first(*keys: str) -> str:
    for source in (shell_env, local_env, base_env, azd_env):
        for key in keys:
            value = source.get(key)
            if value:
                return value.strip()
    return ""


def first_selected_env(*keys: str) -> str:
    for source in (shell_env, azd_env, local_env, base_env):
        for key in keys:
            value = source.get(key)
            if value:
                return value.strip()
    return ""


def first_non_placeholder_endpoint(*keys: str) -> str:
    fallback = ""
    for source in (shell_env, local_env, base_env, azd_env):
        for key in keys:
            value = (source.get(key) or "").strip()
            if not value:
                continue
            if not fallback:
                fallback = value
            if "your-resource.openai.azure.com" not in value:
                return value
    return fallback


foundry = first_selected_env(
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT",
    "ai_foundry_project_endpoint",
)
subscription = first("AZURE_SUBSCRIPTION_ID")
azure_openai_endpoint = first_non_placeholder_endpoint("AZURE_OPENAI_ENDPOINT")
openai_chat_endpoint = first_non_placeholder_endpoint("OPENAI_CHAT_ENDPOINT")
azure_openai_model = first("OPENAI_CHAT_MODEL", "AZURE_OPENAI_CHAT_DEPLOYMENT_ID", "AZURE_OPENAI_DEPLOYMENT_ID")
azure_openai_key = first("OPENAI_CHAT_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY")
azure_openai_api_version = first("AZURE_OPENAI_API_VERSION") or "2025-01-01-preview"

derived: dict[str, str] = {}
if foundry:
    derived["FOUNDRY_PROJECT_ENDPOINT"] = foundry.rstrip("/")
    derived["AZURE_AI_FOUNDRY_PROJECT_ENDPOINT"] = foundry.rstrip("/")
if subscription:
    derived["AZURE_SUBSCRIPTION_ID"] = subscription
version = first("AGENT_RECALL_AGENT_VERSION") or eval_agent_version()
if version:
    derived["AGENT_RECALL_AGENT_VERSION"] = version
if azure_openai_endpoint:
    derived["AZURE_OPENAI_ENDPOINT"] = azure_openai_endpoint.rstrip("/")
if openai_chat_endpoint and "your-resource.openai.azure.com" not in openai_chat_endpoint:
    derived["OPENAI_CHAT_ENDPOINT"] = openai_chat_endpoint.rstrip("/")
elif azure_openai_endpoint:
    derived["OPENAI_CHAT_ENDPOINT"] = f"{azure_openai_endpoint.rstrip('/')}/openai/v1"
if azure_openai_model:
    derived["OPENAI_CHAT_MODEL"] = azure_openai_model
if azure_openai_key:
    derived["OPENAI_CHAT_KEY"] = azure_openai_key
if azure_openai_api_version:
    derived["AZURE_OPENAI_API_VERSION"] = azure_openai_api_version

required = [
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_SUBSCRIPTION_ID",
    "AGENT_RECALL_AGENT_VERSION",
    "OPENAI_CHAT_ENDPOINT",
    "OPENAI_CHAT_MODEL",
    "OPENAI_CHAT_KEY",
]


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def write_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    seen: set[str] = set()
    pattern = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(=)(.*)$")
    rewritten: list[str] = []
    for line in lines:
        match = pattern.match(line)
        if not match:
            rewritten.append(line)
            continue
        prefix, key, equals, _raw = match.groups()
        if key in updates:
            rewritten.append(f"{prefix}{key}{equals}{shell_quote(updates[key])}")
            seen.add(key)
        else:
            rewritten.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if rewritten and rewritten[-1].strip():
            rewritten.append("")
        rewritten.extend(
            [
                "# ============================================================================",
                "# AgentOps / ASSERT / Red Team pinned endpoints",
                "# Managed by devops/agentops/hydrate-gate-env.sh. Do not store Azure tokens here.",
                "# ============================================================================",
            ]
        )
        for key in missing:
            rewritten.append(f"{key}={shell_quote(updates[key])}")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


if write_env and not check_only:
    write_env_file(env_file, derived)

if write_azd and foundry and not check_only:
    for key in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT"):
        subprocess.run(["azd", "env", "set", key, foundry.rstrip("/")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
if write_azd and subscription and not check_only:
    subprocess.run(["azd", "env", "set", "AZURE_SUBSCRIPTION_ID", subscription], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
if write_azd and version and not check_only:
    subprocess.run(["azd", "env", "set", "AGENT_RECALL_AGENT_VERSION", version], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

if print_exports:
    for key in required + ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION"]:
        value = derived.get(key)
        if value:
            print(f"export {key}={shell_quote(value)}")
else:
    for key in required:
        print(f"{key}={'SET' if derived.get(key) else 'missing'}")
    if write_env and not check_only:
        print(f"env_file_updated={env_file}")
    if write_azd and foundry and not check_only:
        print("azd_foundry_aliases=SET")

missing = [key for key in required if not derived.get(key)]
if missing:
    sys.exit(2)
PY