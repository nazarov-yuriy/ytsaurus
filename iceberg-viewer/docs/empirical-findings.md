# Empirical findings from driving the real UI against the mock

These details were discovered by running ytsaurus-ui (headless Chromium) against
`mock-backend-py/` and watching what broke. They are requirements beyond what
static code reading surfaced, verified 2026-07-25 against ytsaurus-ui@3.20.0.

## Hard requirements found by testing

1. **`TYPED_OUTPUT_FORMAT` must be honored.** The navigation preload sends
   `execute_batch` with `output_format: {"$value":"json","$attributes":{"annotate_with_types":true,"stringify":true}}`.
   The backend must wrap **every scalar** in the result (including inside batch
   sub-results) as `{"$type":"int64|double|boolean|string","$value":"<stringified>"}`.
   With plain scalars the UI's `prepareAttributes` yields `type: undefined` and the page
   shows "Viewing node of type unknown is not supported in navigator."

2. **`@path` attribute must be absolute with a double slash** (`//home/...`, root `/`).
   A single-slash path makes `ypath.js:91` throw
   `invalid relative ypath "..." - fragment should start with action modifier`.

3. **`@key_columns` must exist on every table node** (list of sorted column names, `[]`
   for unsorted). `Columns.getKeyColumns` (utils/navigation/content/table/columns.js:67)
   reads it and `prepareColumns` crashes with
   `Cannot read properties of undefined (reading 'indexOf')` when it is missing.
   `@sorted_by` should match.

4. **`get //sys/pool_trees/@default_tree`** is issued on every navigation load
   (`suppress_access_tracking: true`); failure produces a visible error toast.
   Provide a `pool_trees` node with attribute `default_tree` (e.g. `"physical"`).

5. **`POST /api/v4/get {"path": "<table>/@has_row_level_ace"}`** is issued when opening
   a table; provide the attribute (`false`) to avoid a 400. Note the v4 envelope:
   `{"value": false}`.

6. **`secrets/yt-interface-secret.json` must exist** whenever the config sets
   `ytInterfaceSecret` (the default `development` config does) — `configure-app.ts:11`
   `require`s it at boot, even with `authentication: "none"`. Content `{}` suffices.

7. Attribute reads observed at table-open time that must not 500 the page (errors with
   code 500 are tolerated per-attribute): `@mount_config`, `@annotation`,
   `@annotation_path`, `@type`, `@dynamic`, plus the ~70-attribute `get <path>/@` batch.

## Request sequence observed (navigation → table open)

```
GET  /api/cluster-info/mock            (UI server → proxy: /auth/whoami + /version)
GET  /api/cluster-params/mock          (UI server → proxy: 2× execute_batch, //sys/media gate)
POST /api/yt/mock/api/v3/execute_batch {get <path>/@, attributes:[~70 names], TYPED format}
POST /api/yt/mock/api/v3/execute_batch {get @type, get @dynamic}
POST /api/yt/mock/api/v3/get           {path: //sys/pool_trees/@default_tree}
GET  /api/yt/mock/api/v3/get_supported_features
POST /api/yt/mock/api/v3/list          {path, attributes} (map nodes)
POST /api/yt/mock/api/v3/check_permission {permission: full_read}
POST /api/yt/mock/api/v4/get           {path: <table>/@has_row_level_ace}
POST /api/yt/mock/api/v3/execute_batch {get <table>/@mount_config}
POST /api/yt/mock/api/v3/read_table    {path: <t>[#0:#0], web_json, column_names: []}   ← column discovery
POST /api/yt/mock/api/v3/read_table    {path: <t>[#0:#51], web_json, max_selected_column_count: 50}
```

## Operational notes

- Dev server: `LOCAL_DEV_PORT=8080 npm run dev:app` in `packages/ui`; the Rspack dev server
  on 8080 proxies to the Node app on 8081. If the app crashes at boot (e.g. missing
  secrets file), the watcher does NOT restart it — rerun `npm run dev:app`.
- Node 20 works in practice despite `"engines": {"node": ">=24"}`.
- Verified with Playwright headless Chromium (`npx playwright install chromium`,
  no system deps needed). The tracked driver is `recordings/play.js`; from
  `recordings/`, run
  `NODE_PATH=../ytsaurus-ui/packages/ui/node_modules node play.js`.
  For ad-hoc debugging, also capture `pageerror`, `requestfailed`, and responses with
  status ≥ 400.
