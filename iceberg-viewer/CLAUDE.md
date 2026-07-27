# iceberg-viewer

Reuse ytsaurus-ui as an Apache Iceberg catalog viewer. This directory contains
the reverse-engineered YT HTTP-proxy protocol (docs + SQLite catalog), a Python
mock backend that serves the real UI, recorded-traffic test corpora, and
Helm/compose deployment. `mock-backend-py/data.py` is the designated swap
point for a real Iceberg catalog; everything else is meant to stay.

## Environment setup

```bash
# Backend + tests (FastAPI/uvicorn HTTP layer — stdlib is no longer enough):
python3 -m venv .venv && .venv/bin/pip install -r mock-backend-py/requirements.txt
.venv/bin/python mock-backend-py/server.py 8000

# PostgreSQL-backed tests skip cleanly without a DSN. Any PostgreSQL >= 14:
MOCK_PG_TEST_DSN='postgresql://user@host:5432/db' .venv/bin/python tests/test_user_persistence.py
# (no system PG? theseus-rs/postgresql-binaries gnu builds work portably;
#  point LD_LIBRARY_PATH at extracted libxml2 if the host lacks it)

# Real UI against the mock (checkout lives at ./ytsaurus-ui):
cd ytsaurus-ui/packages/ui && npm ci        # clusters-config.json already points at localhost:8000
LOCAL_DEV_PORT=8080 npm run dev             # client on 8080, node server on 8081
# headless checks: playwright + chromium are in the UI's node_modules —
#   NODE_PATH=ytsaurus-ui/packages/ui/node_modules node <script>.js

# Deployment validation (no cluster/docker needed): helm + kubeconform in PATH
helm lint deploy/helm/iceberg-ui-mock
bash deploy/helm/iceberg-ui-mock/tests/test-auth-render.sh
```

## Test & consistency gates (all must stay green)

```bash
for t in test_protocol test_userdb test_cookie_model test_slow_backend \
         test_recording_security test_golden_replay test_external_auth; do
  .venv/bin/python tests/$t.py
done
python3 db/sync.py check && python3 db/sync.py audit
```

- `tests/test_golden_replay.py` diffs every recorded UI request against
  `recordings/golden.jsonl` — the wire contract. Regenerate with
  `GOLDEN_UPDATE=1` only for deliberate wire changes, and say so in the commit.
- `db/sync.py` keeps docs ⇄ SQLite catalog ⇄ server surface ⇄ recorded traffic
  synchronized. After adding/changing an endpoint: update the inventory JSON in
  `docs/`, add a `db/support-status.json` rule if needed, run
  `sync.py load && sync.py export && sync.py check && sync.py audit`, and
  commit the regenerated `docs/*-INDEX.md` / `db/api_catalog.sqlite`.
- New real-UI traffic can be swept for uncataloged calls with
  `recordings/play-discovery.js` + `recordings/discover.py`.

## Conventions

- Compact implementations; short comments only for non-obvious constraints.
  Tests/docs/data may be big — logic should not be.
- Wire fidelity beats taste: much of the backend mirrors the real C++ proxy,
  including oddities (YT code 500 inside HTTP 400, the 'CSFR' typo, string
  booleans in web_json). Check `mock-backend-py/REVIEW.md` and `docs/` before
  "fixing" anything that looks wrong; cite the upstream source when mirroring.
- `data.py` must stay deterministic (sequential ids, fixed timestamps) —
  golden replay compares bytes.
- `userdb.py` has parallel PostgreSQL and in-RAM implementations of the same
  API; change both, behavior must match. PG writes use `retry=False`.
- FastAPI route handlers are sync (`def`) on purpose — command logic blocks;
  read request bodies from `request.state.body_buf`, never `await`.
- Audit trail: strict columns (ts/login/endpoint) + schemaless jsonb details,
  written before the response, fail-open, capped < 1000 bytes per row.

## Pitfalls (learned the hard way)

- Killing background servers: `pkill -f "pattern"` matches your own compound
  shell command if it contains the literal pattern — kill by saved PID, or use
  bracket patterns (`[s]erver.py`) only from a *separate* Bash call.
- A stale server holding the port makes "my change didn't take" illusions —
  verify with a probe request after restarts.
- Tests spawn `sys.executable`; run them with the venv interpreter or fastapi
  imports fail at server startup (backend "did not start" in setUpModule).
