#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash devops/agentops/run-clinic-recall-eval-pipeline.sh [options]

Runs the Clinic Recall phone/SMS evaluation evidence loop:
  1. validate canonical synthetic cases and gate JSONL files
  2. run focused deterministic pytest suites
  3. run Ruff hygiene on touched Python surfaces
  4. run Recall Agent and Inbound Assistant AgentOps gates unless skipped

Options:
  --env NAME              azd environment passed to gate runners (default: phase0)
  --skip-agentops         skip hosted AgentOps/ASSERT/Red Team/Doctor gates
  --skip-doctor           skip Doctor in both hosted gate runs
  --doctor-nonblocking    do not fail the pipeline on Doctor exit code
  -h, --help              show this help
EOF
}

log() {
    printf '\n==> %s\n' "$*"
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
azd_env="${AZURE_ENV_NAME:-phase0}"
skip_agentops=0
skip_doctor=0
doctor_nonblocking=0

while (($#)); do
    case "$1" in
        --env)
            [[ $# -ge 2 ]] || { echo "--env requires a value" >&2; exit 1; }
            azd_env="$2"
            shift 2
            ;;
        --skip-agentops)
            skip_agentops=1
            shift
            ;;
        --skip-doctor)
            skip_doctor=1
            shift
            ;;
        --doctor-nonblocking)
            doctor_nonblocking=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

cd "$repo_root"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$repo_root/.uv-cache}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

canonical_cases=".agentops/data/clinic-recall-phone-sms-cases.jsonl"
smoke_files=(
    ".agentops/data/recall-smoke.jsonl"
    ".agentops/data/inbound-smoke.jsonl"
)
assert_files=(
    "assert/test_set.jsonl"
    "assert/inbound_test_set.jsonl"
)
pytest_files=(
    "tests/test_clinic_recall_eval_cases.py"
    "tests/test_clinic_recall_sms_intent_eval.py"
    "tests/test_clinic_recall_sms_webhook_integration.py"
    "tests/test_clinic_recall_messaging_inbound.py"
    "tests/test_clinic_recall_booking.py"
    "tests/test_inbound_clinic_tools.py"
    "tests/test_voicelive_clinic_recall_safety.py"
)
ruff_files=(
    "src/clinic_recall/eval_cases.py"
    "src/clinic_recall/inbound_messages.py"
    "apps/artagent/backend/api/v1/endpoints/sms.py"
    "tests/test_clinic_recall_eval_cases.py"
    "tests/test_clinic_recall_sms_webhook_integration.py"
    "tests/test_voicelive_clinic_recall_safety.py"
)

log "Validating canonical synthetic evaluation cases"
uv run python -m src.clinic_recall.eval_cases "$canonical_cases"

log "Validating smoke gate JSONL rows"
uv run python -m src.clinic_recall.eval_cases --smoke "${smoke_files[@]}"

log "Validating ASSERT JSONL rows"
uv run python - "${assert_files[@]}" <<'PY'
import json
import sys
from pathlib import Path

for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    case_ids = [row.get("test_case_id") for row in rows]
    if len(case_ids) != len(set(case_ids)):
        raise SystemExit(f"{path}: duplicate test_case_id")
    for index, row in enumerate(rows, start=1):
        missing = {"type", "behavior", "seed", "dimensions", "test_case_id"} - set(row)
        if missing:
            raise SystemExit(f"{path}:{index}: missing {', '.join(sorted(missing))}")
    print(f"{path}: {len(rows)} rows")
PY

log "Running focused deterministic pytest suites"
uv run pytest "${pytest_files[@]}" -q

log "Running Ruff hygiene on touched Python files"
uv run ruff check "${ruff_files[@]}"

if (( skip_agentops )); then
    log "Skipping hosted AgentOps gates by request"
else
    gate_args=(--env "$azd_env")
    if (( skip_doctor )); then
        gate_args+=(--skip-doctor)
    elif (( doctor_nonblocking )); then
        gate_args+=(--doctor-nonblocking)
    fi

    log "Running Recall Agent hosted gate"
    bash devops/agentops/run-recall-agent-gate.sh "${gate_args[@]}"

    log "Running Inbound Assistant hosted gate"
    bash devops/agentops/run-recall-agent-gate.sh \
        --agent-dir src/inbound-assistant \
        --config agentops-inbound.yaml \
        "${gate_args[@]}"
fi

log "Clinic Recall phone/SMS evaluation pipeline complete"