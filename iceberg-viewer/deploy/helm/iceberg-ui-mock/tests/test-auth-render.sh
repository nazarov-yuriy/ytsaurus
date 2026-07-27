#!/usr/bin/env bash
set -euo pipefail

chart_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
helm_bin=${HELM_BIN:-helm}
safe_test_token=auth-regression-only-9f187c83e48e4ca8
test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

assert_present() {
    local file=$1
    local text=$2
    grep -Fq -- "$text" "$file" ||
        fail "expected rendered chart to contain: $text"
}

assert_absent() {
    local file=$1
    local text=$2
    if grep -Fq -- "$text" "$file"; then
        fail "expected rendered chart not to contain: $text"
    fi
}

command -v "$helm_bin" >/dev/null ||
    fail "Helm executable not found: $helm_bin"

"$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression >"$test_tmp/default.yaml"
assert_present "$test_tmp/default.yaml" '"authentication":"none"'
assert_absent "$test_tmp/default.yaml" '            - name: ALLOW_PASSWORD_AUTH'
assert_absent "$test_tmp/default.yaml" '            - name: MOCK_REQUIRE_AUTH'
assert_absent "$test_tmp/default.yaml" '            - name: MOCK_ENABLE_DEV_SEED_USERS'

if "$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set-string auth.ytUpstream=https://proxy.yt.example \
    >"$test_tmp/default-token.yaml" 2>"$test_tmp/default-token.stderr"; then
    fail "authenticated mode rendered with the published default robot token"
fi
assert_present "$test_tmp/default-token.stderr" \
    'auth.robotToken must be changed from the published mock-robot-token default'

"$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set-string auth.ytUpstream=https://proxy.yt.example \
    --set-string "auth.robotToken=$safe_test_token" \
    >"$test_tmp/upstream.yaml"
assert_present "$test_tmp/upstream.yaml" '"authentication":"basic"'
assert_present "$test_tmp/upstream.yaml" '            - name: ALLOW_PASSWORD_AUTH'
assert_present "$test_tmp/upstream.yaml" '            - name: MOCK_YT_UPSTREAM'
assert_present "$test_tmp/upstream.yaml" '            - name: MOCK_REQUIRE_AUTH'
assert_present "$test_tmp/upstream.yaml" '        - name: ROBOT_TOKEN'
assert_absent "$test_tmp/upstream.yaml" '            - name: MOCK_ENABLE_DEV_SEED_USERS'
assert_absent "$test_tmp/upstream.yaml" '-u iceberg:iceberg'

if "$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set-string auth.ytUpstream=https://proxy.yt.example \
    --set-string ui.cluster.authentication=none \
    --set-string "auth.robotToken=$safe_test_token" \
    >"$test_tmp/invalid.yaml" 2>"$test_tmp/invalid.stderr"; then
    fail "auth.ytUpstream with authentication=none rendered successfully"
fi
assert_present "$test_tmp/invalid.stderr" \
    'ui.cluster.authentication=none is incompatible with non-empty auth.ytUpstream'

printf 'Helm authentication rendering checks passed\n'
