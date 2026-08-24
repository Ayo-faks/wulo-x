#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT_DIR"

UV_BIN=${UV_BIN:-$(command -v uv || true)}
if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
    echo "uv executable not found" >&2
    exit 1
fi

REQUIRED_UV_VERSION=0.7.19
ACTUAL_UV_VERSION=$("$UV_BIN" --version | awk '{print $2}')
if [[ "$ACTUAL_UV_VERSION" != "$REQUIRED_UV_VERSION" ]]; then
    echo "Python image locks require uv $REQUIRED_UV_VERSION; found $ACTUAL_UV_VERSION" >&2
    exit 1
fi

TMP_BASE=${TMPDIR:-/tmp}
TMP_ROOT=$(mktemp -d "${TMP_BASE%/}/clinic-recall-python-locks.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT

cp src/recall-agent/requirements.lock "$TMP_ROOT/recall-agent.lock"
"$UV_BIN" pip compile src/recall-agent/requirements.txt \
    --no-config \
    --generate-hashes \
    --no-header \
    --no-upgrade \
    --python-version 3.13 \
    --python-platform x86_64-unknown-linux-gnu \
    --output-file "$TMP_ROOT/recall-agent.lock" \
    > /dev/null
cp src/inbound-assistant/requirements.lock "$TMP_ROOT/inbound-assistant.lock"
"$UV_BIN" pip compile src/inbound-assistant/requirements.txt \
    --no-config \
    --generate-hashes \
    --no-header \
    --no-upgrade \
    --python-version 3.13 \
    --python-platform x86_64-unknown-linux-gnu \
    --output-file "$TMP_ROOT/inbound-assistant.lock" \
    > /dev/null
cp apps/cardapi/mcp_app/requirements.lock "$TMP_ROOT/cardapi.lock"
"$UV_BIN" pip compile apps/cardapi/mcp_app/requirements.txt \
    --no-config \
    --generate-hashes \
    --no-header \
    --no-upgrade \
    --python-version 3.11 \
    --python-platform x86_64-unknown-linux-gnu \
    --output-file "$TMP_ROOT/cardapi.lock" \
    > /dev/null
"$UV_BIN" export \
    --locked \
    --no-dev \
    --no-editable \
    --no-emit-project \
    --format requirements-txt \
    > "$TMP_ROOT/backend.lock"

check_lock() {
    generated=$1
    tracked=$2
    if ! cmp -s "$generated" "$tracked"; then
        echo "Stale Python image lock: $tracked" >&2
        diff -u "$tracked" "$generated" || true
        return 1
    fi
}

check_compiled_lock() {
    generated=$1
    tracked=$2
    if ! cmp -s "$generated" <(tail -n +3 "$tracked"); then
        echo "Stale Python image lock: $tracked" >&2
        diff -u <(tail -n +3 "$tracked") "$generated" || true
        return 1
    fi
}

check_compiled_lock "$TMP_ROOT/recall-agent.lock" src/recall-agent/requirements.lock
check_compiled_lock "$TMP_ROOT/inbound-assistant.lock" src/inbound-assistant/requirements.lock
check_compiled_lock "$TMP_ROOT/cardapi.lock" apps/cardapi/mcp_app/requirements.lock
check_lock "$TMP_ROOT/backend.lock" apps/artagent/backend/requirements.lock

echo "Python image locks are current."
