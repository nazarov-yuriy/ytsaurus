#!/bin/bash
# Runs inside the `tests` compose service: every self-contained backend suite
# (they spawn their own servers in this container) plus the end-to-end smoke
# against the composed ui + mock-backend + postgres.
set -e
cd /workspace

echo '== installing pinned PG-mode dependencies'
python3 -m pip install --quiet --requirement mock-backend-py/requirements.txt

for suite in test_protocol test_userdb test_cookie_model test_slow_backend test_golden_replay; do
    echo "== tests/$suite.py"
    python3 "tests/$suite.py"
done

echo '== tests/test_user_persistence.py (against composed postgres)'
python3 tests/test_user_persistence.py

echo '== catalog consistency (docs<->DB<->server surface)'
API_CATALOG_DB=/tmp/api_catalog.sqlite python3 db/sync.py load > /dev/null
API_CATALOG_DB=/tmp/api_catalog.sqlite python3 db/sync.py audit

echo '== end-to-end smoke against composed ui + mock-backend'
python3 deploy/compose/compose-smoke.py

echo 'ALL COMPOSE TESTS PASSED'
