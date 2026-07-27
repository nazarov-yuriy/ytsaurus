# Reusing the YTsaurus UI as an Apache Iceberg catalog viewer

Working area for documenting the YTsaurus backend ↔ frontend protocol and building a
mock backend, as a first step toward serving the UI from an Apache Iceberg catalog.

## Layout

- `ytsaurus-ui/` — shallow clone of https://github.com/ytsaurus/ytsaurus-ui (the frontend:
  React app + its Node/Express server). The real backend it talks to is the C++ HTTP proxy
  in this repo (`yt/yt/server/http_proxy`).
- `docs/` — protocol documentation (**start at `docs/INDEX.md`** — the generated map of
  every doc and its cross-references):
  - `auth.md` — login, cookies, CSRF, token flow.
  - `table-viewer.md` — navigation & static-table viewing wire protocol (get/list/exists,
    `read_table` with `web_json`, error format).
  - `bootstrap-config.md` — clusters-config.json, UI server routes, how to run the UI.
  - `coverage-notes.md` — protocol-wide conventions + out-of-scope endpoint rationale.
  - `timeouts.md` — the timeout on every layer of the request path, the slow-catalog
    budget (data ≤100s, `//sys` boot path ≤5s), and the `MOCK_DELAY` simulation switch.
  - `iceberg-considerations.md` — brainstorm for the real Iceberg backend: data-model
    and type mapping, auth, feature trimming, contracts to preserve, open questions.
  - `*.inventory.json` — machine-readable API inventories (source of truth for the DB).
  - `API-INDEX.md`, `ENTITIES.md` — generated from the SQLite DB, do not edit.
- `db/` — structured API catalog:
  - `api_catalog.sqlite` — committed generated catalog consumed by sanity checks
    and recording coverage analysis.
  - `schema.sql` — SQLite schema (endpoints, params, entities/fields with
    support_status, MD coverage, recorded_requests).
  - `entities.json` — hand-curated payload entities (yt-error, web_json, cluster-info,
    …) with per-field support status.
  - `node-attributes.generated.json` — the node-attributes entity (86 fields), computed
    from the play-session recordings ∪ mock data.
  - `support-status.json` — ordered rules assigning each endpoint
    `implemented` (dynamic, reimplement over Iceberg) / `constant` (stubbed, keep) /
    `unused` (not needed).
  - `sync.py` — `load` (inventories + entities + statuses → DB), `export`
    (DB → generated `API-INDEX.md` + `ENTITIES.md` + `INDEX.md`), `check` (fails
    unless every endpoint is mentioned in a handwritten doc, everything has a
    status, and the generated MD matches the DB), `audit` (fails when the
    catalog's implemented/constant claims drift from the actual
    `mock-backend-py/server.py` surface), `query "SQL"`.
- `mock-backend-py/` — Python HTTP server mimicking the YT HTTP proxy with in-RAM
  fake catalog data and optional PostgreSQL-backed users and sessions
  (`python3 server.py 8000`); see its README for configuration and implementation
  notes.
- `tests/test_protocol.py` — documented-behavior conformance tests for the backend
  (`python3 tests/test_protocol.py`).
- `tests/test_userdb.py` — always-running PBKDF2, session-revocation, and reconnect
  unit tests; `test_user_persistence.py` adds isolated PostgreSQL integration
  coverage when `MOCK_PG_TEST_DSN` is available.
- `tests/test_external_auth.py` — delegated authentication against a real
  YTsaurus (`MOCK_YT_UPSTREAM`): external users provisioned on first verified
  login, explicitly provisioned local users never leaving the local store
  (docs/auth.md "External authentication").
- `tests/test_recording_security.py` — proves development traffic recordings
  redact credentials, fail open on filesystem errors, stay size-bounded, and
  cannot be enabled alongside strict or delegated authentication.
- `tests/test_golden_replay.py` — replays the recorded UI corpus
  (`recordings/proxy-traffic.jsonl`) against the backend and diffs every response
  with `recordings/golden.jsonl` (the wire contract with the UI; regenerate with
  `GOLDEN_UPDATE=1` after deliberate changes).
- `docker-compose.yml` — the whole stack in containers (UI + mock + PostgreSQL),
  nothing on the host: `docker compose up --build`, then
  `docker compose run --rm tests` for every suite; see `deploy/README.md`.
- `deploy/` — Kubernetes deployment: Helm chart (`helm/iceberg-ui-mock`) running
  UI + mock together (modeled on the official ui-helm-chart), a Dockerfile for a
  baked backend image, and a `helm test` smoke suite; see `deploy/README.md`.

## Architecture being mocked

```
browser (React app)
   │  /api/... (UI's own endpoints: config, login forwarding, settings)
   ▼
UI Node/Express server (packages/ui/src/server)
   │  /api/v3|v4/<command>  (+ /hosts, /ping) — YT HTTP proxy protocol
   ▼
cluster HTTP proxy  ◀── this is what mock-backend-py/ replaces
```
