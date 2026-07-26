#!/bin/bash
# Runs inside the `tests` compose service: every self-contained backend suite
# (they spawn their own servers in this container) plus the end-to-end smoke
# against the composed ui + mock-backend + postgres.
set -e
cd /workspace

echo '== installing psycopg (PG-mode suites)'
pip install --quiet 'psycopg[binary]'

for suite in test_protocol test_userdb test_cookie_model test_slow_backend test_golden_replay; do
    echo "== tests/$suite.py"
    python3 "tests/$suite.py"
done

echo '== tests/test_user_persistence.py (against composed postgres)'
python3 tests/test_user_persistence.py

echo '== end-to-end smoke against composed ui + mock-backend'
python3 deploy/compose/compose-smoke.py

echo 'ALL COMPOSE TESTS PASSED'
