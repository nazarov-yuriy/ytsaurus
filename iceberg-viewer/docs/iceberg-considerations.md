# Reusing ytsaurus-ui for an Apache Iceberg viewer — considerations & open questions

Brainstorm informed by building the mock stack. Tags: **[verified]** — confirmed in
this project's code/tests; **[likely]** — strong evidence, not fully tested;
**[uncertain]** — needs investigation; **[idea]** — design option, not validated.

## 1. Data-model mapping (Cypress ⇄ Iceberg)

- **[verified]** The swap point is exactly one module (`mock-backend-py/data.py`):
  namespaces → `map_node`s, tables → `table` nodes. Everything protocol-side stays.
- **[idea]** Multiple Iceberg **catalogs → multiple "clusters"** in
  clusters-config.json. The UI's cluster switcher becomes a catalog switcher for
  free; each catalog gets its own color theme/environment badge.
- **[likely]** Attribute sourcing from snapshot metadata: `row_count` and
  `chunk_row_count` ← `total-records`, sizes ← `total-files-size`,
  `modification_time` ← current snapshot timestamp, `creation_time` ← first
  metadata-log entry. Note **the table viewer pages by `chunk_row_count`, not
  `row_count`** [verified] — keep them equal.
- **[uncertain]** `total-records` semantics with v2 **position/equality deletes**:
  summary counts may overstate live rows. Paging past the real end must return
  empty rows gracefully (the UI tolerates short pages [likely]).
- **[uncertain]** **YPath-special characters in Iceberg identifiers** (`/ @ & * [ ]`
  are meaningful in YPath; Iceberg allows more than YT node names do). Options:
  YPath literal escaping, percent-encoding in the backend, or rejecting such
  tables with a clear error. Multi-level namespaces map naturally to nested dirs,
  but a namespace literally named `a/b` collides. Needs a canonical encoding.
- **[idea]** Surface Iceberg-only metadata (partition spec, table properties,
  format-version, current-snapshot-id, manifests count) via **`@user_attributes`**
  — the tab renders arbitrary maps [verified: tab probes `@user_attributes`].
  YT's `annotation` attribute renders markdown on the node page [likely] — usable
  for table descriptions/comments.

## 2. Schema & type mapping

- **[verified]** The viewer needs `@schema` (legacy `type` + `type_v3` per column),
  `@key_columns`, `@sorted_by`, `@dynamic:false`, `@schema_mode:strong`.
- **[likely]** Iceberg primitives map cleanly: int/long → int32/int64,
  float/double, string, boolean, date/time/timestamp[tz] → type_v3
  `date`/`timestamp`/... , binary → `string` (yson-safe), uuid → `utf8`.
- **[uncertain]** `decimal(p,s)`: type_v3 has `decimal`, but which web_json `$type`
  the UI renders for cell values is untested — a string fallback is safe.
- **[verified]** Nested types (struct/list/map) work as legacy `any` +
  JSON-stringified cells (the `events.payload` column proves the render path).
- **[verified]** Richer complex-type rendering via `value_format: "yql"` is now
  implemented in both mocks (`yql_type_registry` + `[value, type_index]` cells,
  per table-viewer.md §5.4): the UI requests it and renders nested any/Yson
  values as an expandable tree. Base64 (`b64`), truncation (`inc`), and native
  Struct/List/Dict registry types remain uncovered.
- **[uncertain]** Iceberg **sort orders → `key_columns`/`sorted_by`** is honest;
  mapping **partition fields** there is not (different semantics). Partition spec
  probably belongs in user attributes; whether the UI can show a per-column
  "partition" badge without forking is doubtful.

## 3. Reading data (the real engine)

- **[likely]** pyiceberg scan → Arrow → web_json is the natural pipeline:
  `column_names` maps to projection (efficient); row ranges `[#a:#b]` have **no
  offset pushdown in Iceberg** — deep offsets mean scan-and-skip. Consider
  capping the offset (UI paging to row 10M would be pathological) or
  implementing per-file row-index arithmetic from manifest entry counts
  (only exact when no deletes) [idea].
- **[verified]** Truncation exists in the protocol: `field_weight_limit`/
  `string_weight_limit` + `$incomplete` cells. Worth implementing for real data
  (a 100MB binary cell must not reach the browser); the mock never truncates.
- **[idea]** Cache namespace listings and table metadata (REST catalogs can be
  slow); snapshot-id-keyed caches invalidate naturally. An async refresh with
  slightly stale listings beats a 30s navigation click.

## 4. Auth & authorization

- **[verified]** Three working modes exist today: anonymous (`authentication:
  none`), password+cookie backed by PostgreSQL, and a robot OAuth token. The UI
  also supports OIDC-style OAuth (`ytOAuthSettings`) — the right target for
  company SSO [likely, config documented in auth.md §7 but untested here].
- **[uncertain]** **Per-user catalog credentials**: the backend currently talks to
  the catalog with one service identity. Passing the UI user's identity through
  to catalog-level RBAC (Polaris/Unity/Glue) is an open design question —
  token exchange? per-user REST sessions?
- **[verified]** `check_permission` is consulted by the UI; the mock always
  allows. For read-only Iceberg, deny `write`/`remove`/`administer` so the UI
  greys out mutating actions — **whether every write control honors a deny needs
  a click-through audit** [uncertain]; the backend must reject writes regardless
  (unimplemented commands already 404 [verified]).
- **[uncertain]** The ACL tab renders `@acl`/`@effective_acl` (empty in mock).
  Mapping catalog grants into YT ACE format for display is possible but the
  fidelity is questionable; hiding the tab may need UI changes.

## 5. Trimming YTsaurus-only surface

- **[verified]** Dead pages remain reachable (Queries, Operations, Accounts,
  Scheduling, Bundles) and harmless-but-ugly artifacts exist: "QT Kit" button in
  the table toolbar, Create object, Upload. Their backends are `unused` in the
  catalog and degrade to toasts/empty states.
- **[likely]** The official customization path is **`UIFactory`** (~500-line
  interface, `src/ui/UIFactory/`) + the **`custom-ytsaurus-ui.example`** package —
  build a custom app that overrides menus/pages/tabs without forking. Which
  exact hooks hide top-level pages needs a spike; `uiConfig` from
  `//sys/@ui_config` also carries feature flags the mock could serve
  (`@ui_config` is already requested at boot [verified], flag names not mapped).
- **[idea]** Iceberg **time travel** deserves first-class UI eventually:
  snapshots as virtual child nodes (`//catalog/db/table/@snapshots` or a
  `.snapshots/<id>` subtree the viewer can browse) works with zero UI changes;
  a real snapshot picker in the table toolbar means UIFactory work.
- **[uncertain]** Iceberg **views**: map to YT `document` or `view`-ish node
  types? The navigator supports several node kinds; which render acceptably is
  untested.

## 6. Protocol contracts to preserve (hard-won)

All encoded in `tests/test_protocol.py` + `recordings/` — run them against any
new backend implementation, they are the real spec:

- **[verified]** `TYPED_OUTPUT_FORMAT` scalar annotation; `@path` `//`-absolute;
  `@key_columns` mandatory; virtual attrs (`@user_attributes`,
  `@opaque_attribute_keys`); typed batch envelopes; v4 `{value}` wrap;
  web_json string-typed `incomplete_*` flags; error envelope + X-YT-* headers;
  login branch statuses; explicit `Connection:` headers; chunked request bodies;
  listen backlog ≥ page-load burst; per-request `Connection: close` clients.
- **[verified]** The UI evolves: after upgrading the pinned `ghcr.io/ytsaurus/ui`
  tag, re-run `recordings/play.js` + `analyze.py` and diff COVERAGE.md — new
  endpoints/attributes show up as unimplemented-command log lines (`!!`) and
  corpus diffs. Treat the replay corpus as a contract test against UI upgrades.

## 7. Operations & scale

- **[verified]** The backend is stateless except PostgreSQL users/sessions →
  horizontal scaling is safe today. A real Iceberg backend stays stateless if
  caches are per-pod or external.
- **[idea]** Add request metrics/structured logs before real users (the mock
  logs to stdout only); the `X-YT-Correlation-Id` the UI server already sends
  [verified] is the natural trace key.
- **[uncertain]** Huge namespaces (10k+ tables): the navigation `list` sends the
  full child list; UI-side virtualization quality at that size is untested.
  `max_all_column_names_count` caps exist for columns [verified], not children.
- **[likely]** Production serving should keep nginx's 100s proxy timeouts in
  sync with any changed backend budget (docs/timeouts.md table).

## 8. Licensing & branding

- **[likely]** ytsaurus-ui is Apache-2.0 — reuse and rebranding are fine with
  attribution; double-check the LICENSE of bundled assets (logos are YTsaurus
  trademarks — replace via UIFactory branding hooks in a custom build).

## 9. Suggested next steps (ordered)

1. Spike `data.py` → pyiceberg against a real REST catalog (read-only, one
   namespace).
2. Click-through audit with `check_permission` denying writes; list surviving
   mutating controls.
3. UIFactory spike: hide dead pages, rebrand, decide fork-vs-custom-package.
4. Type-fidelity pass: decimal/timestamp and native composite YQL types.
5. Identifier-encoding decision (YPath-special characters) before any real
   catalog is attached.
