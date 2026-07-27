#!/usr/bin/env bash
set -euo pipefail

chart_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
helm_bin=${HELM_BIN:-helm}
safe_test_token=auth-regression-only-9f187c83e48e4ca8
safe_database_password=database-regression-only-481cd126704c4460
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

if "$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    >"$test_tmp/unconfigured.yaml" 2>"$test_tmp/unconfigured.stderr"; then
    fail "chart rendered without authentication or an anonymous-mode opt-in"
fi
assert_present "$test_tmp/unconfigured.stderr" \
    'explicitly opt in to development-only anonymous mode'

"$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set auth.allowAnonymous=true >"$test_tmp/default.yaml"
assert_present "$test_tmp/default.yaml" '"authentication":"none"'
assert_absent "$test_tmp/default.yaml" '            - name: ALLOW_PASSWORD_AUTH'
assert_absent "$test_tmp/default.yaml" '            - name: MOCK_REQUIRE_AUTH'
assert_absent "$test_tmp/default.yaml" '            - name: MOCK_ENABLE_DEV_SEED_USERS'
assert_absent "$test_tmp/default.yaml" '            - name: MOCK_CORS_ORIGINS'

"$helm_bin" template cors-regression "$chart_dir" \
    --namespace auth-regression \
    --set auth.allowAnonymous=true \
    --set-string 'mockBackend.corsOrigins[0]=https://viewer.internal' \
    >"$test_tmp/cors.yaml"
assert_present "$test_tmp/cors.yaml" '            - name: MOCK_CORS_ORIGINS'
assert_present "$test_tmp/cors.yaml" '              value: "https://viewer.internal"'

if "$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set-string auth.ytUpstream=https://proxy.yt.example \
    >"$test_tmp/default-token.yaml" 2>"$test_tmp/default-token.stderr"; then
    fail "authenticated mode rendered with the published default robot token"
fi
assert_present "$test_tmp/default-token.stderr" \
    'auth.robotToken must be changed from the published mock-robot-token default'

if "$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set-string ui.cluster.authentication=basic \
    >"$test_tmp/basic-without-verifier.yaml" \
    2>"$test_tmp/basic-without-verifier.stderr"; then
    fail "basic authentication rendered without a user store or upstream verifier"
fi
assert_present "$test_tmp/basic-without-verifier.stderr" \
    'authentication=basic requires postgres.enabled=true or non-empty auth.ytUpstream'

if "$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set postgres.enabled=true \
    --set-string "auth.robotToken=$safe_test_token" \
    >"$test_tmp/default-database-password.yaml" \
    2>"$test_tmp/default-database-password.stderr"; then
    fail "PostgreSQL rendered with the published default database password"
fi
assert_present "$test_tmp/default-database-password.stderr" \
    'postgres.password must be changed from the published mock-password default'

"$helm_bin" template auth-regression "$chart_dir" \
    --namespace auth-regression \
    --set postgres.enabled=true \
    --set-string "postgres.password=$safe_database_password" \
    --set-string "auth.robotToken=$safe_test_token" \
    >"$test_tmp/postgres.yaml"
assert_present "$test_tmp/postgres.yaml" '            - name: MOCK_PG_DSN'
assert_present "$test_tmp/postgres.yaml" '            - name: MOCK_REQUIRE_AUTH'
assert_absent "$test_tmp/postgres.yaml" '            - name: MOCK_ENABLE_DEV_SEED_USERS'
assert_absent "$test_tmp/postgres.yaml" 'mock-password'

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
