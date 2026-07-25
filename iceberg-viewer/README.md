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
  - `*.inventory.json` — machine-readable API inventories (source of truth for the DB).
  - `API-INDEX.md` — generated from the SQLite DB, do not edit.
- `db/` — structured API catalog:
  - `schema.sql` — SQLite schema (endpoints, params, schemas, fields, MD coverage).
  - `sync.py` — `load` (inventory JSON → DB), `export` (DB → API-INDEX.md),
    `check` (verify every DB endpoint is mentioned in the MD docs), `query "SQL"`.
- `mock-backend/` — Node HTTP server mimicking the YT HTTP proxy with in-RAM fake data:
  - `server.js` — routes, auth, command dispatch (run: `node server.js [port]`).
  - `data.js` — fake Cypress tree + tables (the layer to reimplement over Iceberg).
  - `webjson.js` — YT `web_json` / annotated-JSON encoders.

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
