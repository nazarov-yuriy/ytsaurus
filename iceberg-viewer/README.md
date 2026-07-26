# Reusing the YTsaurus UI as an Apache Iceberg catalog viewer

Working area for documenting the YTsaurus backend ↔ frontend protocol and building a
mock backend, as a first step toward serving the UI from an Apache Iceberg catalog.

## Layout

- `ytsaurus-ui/` — shallow clone of https://github.com/ytsaurus/ytsaurus-ui (the frontend:
  React app + its Node/Express server). The real backend it talks to is the C++ HTTP proxy
  in this repo (`yt/yt/server/http_proxy`).
- `docs/` — protocol documentation:
  - `auth.md` — login, cookies, CSRF, token flow.
  - `table-viewer.md` — navigation & static-table viewing wire protocol (get/list/exists,
    `read_table` with `web_json`, error format).
  - `bootstrap-config.md` — clusters-config.json, UI server routes, how to run the UI.
  - `coverage-notes.md` — protocol-wide conventions + out-of-scope endpoint rationale.
  - `timeouts.md` — the timeout on every layer of the request path, the slow-catalog
    budget (data ≤100s, `//sys` boot path ≤5s), and the `MOCK_DELAY` simulation switch.
  - `*.inventory.json` — machine-readable API inventories (source of truth for the DB).
  - `API-INDEX.md`, `ENTITIES.md` — generated from the SQLite DB, do not edit.
- `db/` — structured API catalog:
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
    (DB → generated `API-INDEX.md` + `ENTITIES.md`), `check` (fails unless every
    endpoint is mentioned in a handwritten doc, everything has a status, and the
    generated MD matches the DB), `query "SQL"`.
- `mock-backend/` — Node HTTP server mimicking the YT HTTP proxy with in-RAM fake data:
  - `server.js` — routes, auth, command dispatch (run: `node server.js [port]`).
  - `data.js` — fake Cypress tree + tables (the layer to reimplement over Iceberg).
  - `webjson.js` — YT `web_json` / annotated-JSON encoders.
- `mock-backend-py/` — Python (stdlib-only) port of the mock, wire-identical to the
  Node one (`python3 server.py 8000`); see its README for the porting gotchas.
- `tests/test_protocol.py` — 39 documented-behavior conformance tests, each run
  against BOTH backends (`python3 tests/test_protocol.py`).
- `tests/test_userdb.py` — always-running PBKDF2, legacy-migration, and reconnect
  unit tests; `test_user_persistence.py` adds isolated PostgreSQL integration
  coverage when `MOCK_PG_TEST_DSN` is available.
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
cluster HTTP proxy  ◀── this is what mock-backend/ replaces
```
