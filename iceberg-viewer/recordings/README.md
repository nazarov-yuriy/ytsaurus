# Recorded play-session request corpus

Exact request/response traffic captured while driving ytsaurus-ui through every
interaction an Iceberg viewer needs, at both capture points:

- `proxy-traffic.jsonl` — UI-server → proxy requests, recorded by the mock backend
  (start it with `MOCK_RECORD=<path> python3 ../mock-backend-py/server.py 8000`). Full
  request headers/bodies and response status/bodies.
- `browser-traffic.jsonl` — browser → UI-server `/api/*` requests, extracted from a
  Playwright HAR (the raw 125MB HAR is deleted after extraction).
- `extras.jsonl` — endpoints not reachable by clicking (login, logout, cluster
  versions), recorded via curl.

## Session script

`play.js` (Playwright, headless Chromium) performs: root listing → click into tree →
open table → Schema / Attributes / User attributes / ACL tabs → back to Content →
row paging (URL offset + next-page click) → second table with an `any` column →
nonexistent path (error flow) → map-node attributes.

```bash
NODE_PATH=../ytsaurus-ui/packages/ui/node_modules node play.js
python3 analyze.py
```

## Analysis

`analyze.py` collapses the raw traffic into distinct request *shapes* (method + path +
command + attribute + parameter-key set + output format, batch subcommands included),
writes:

- `corpus.json` — one full representative example per shape, with hit counts and
  observed statuses. This is the reference corpus for designing API tests and checking
  which response shapes the UI actually consumes.
- `COVERAGE.md` — two-way diff against `../db/api_catalog.sqlite`: documented-but-not-
  exercised (split mock-critical vs out-of-scope) and exercised endpoint lists.
- `recorded_requests` table in the SQLite DB (queryable next to `endpoints`).

Result of the 2026-07-25 session: 61 distinct shapes, 110 proxy-side + 119 browser-side
requests, **0 mock-critical proxy endpoints unexercised**, no unexpected error statuses.
