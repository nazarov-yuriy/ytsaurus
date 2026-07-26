# Coverage notes: conventions and out-of-scope endpoints

Companion to the generated `API-INDEX.md` / `ENTITIES.md`. Everything in the catalog
must be mentioned in a handwritten doc (`python3 db/sync.py check` enforces it); this file
covers the two groups that don't belong in the feature docs.

## Protocol-wide conventions (apply to every `/api/v3/*` and `/api/v4/*` route)

These pseudo-entries in the catalog describe cross-cutting behavior, documented in
detail in `table-viewer.md`:

- `/api/v3/*` **error-envelope** — pre-flush errors are the `yt-error` entity as the JSON
  body, mirrored into `X-YT-Error` / `X-YT-Response-Code` / `X-YT-Response-Message`; auth
  failures are 401 (codes 110/111/900), ordinary command errors are 400, and some
  dispatch paths use 403/404/415/503. Errors after a streaming response is flushed use
  HTTP trailers (and `read_table` also requests `dump_error_into_response`).
- `/api/v3/*` **format-negotiation** — the request's `output_format`
  (`{"$value":"json","$attributes":{annotate_with_types, stringify}}`) governs the
  response envelope; `annotate_with_types` wraps every scalar as `{$type,$value}`.
- `/api/v3/*` **parameter-decoding** — parameter precedence: inferred formats ←
  query string ← `X-YT-Parameters` (base64 when numbered `-0/-1`) ← JSON POST body.
  The UI wrapper uses `useBodyForParameters` for the parameter-bearing commands on the
  normal navigation/table-display path.
- `OPTIONS /api/v3/*` **CORS-preflight** — served by the proxy but irrelevant in the
  default navigation/table-display topology (the UI server tunnels those browser calls;
  an explicitly enabled direct-download path is the exception).

## Feature endpoints outside the Iceberg viewer's data path

The navigation shell probes access-log availability even though the viewer does not use
the access-log feature. These endpoints therefore have `support_status = constant`:

- `/api/access-log/:ytAuthCluster/check-available`, `/api/access-log/:ytAuthCluster/:action` — access log tab
  (the play corpus observes the check on every navigation load; answer
  `{"is_access_log_available": false}` to hide the tab without an error toast).

The remaining UI-server service routes have `support_status = unused`; they serve
pages/features an Iceberg catalog viewer does not need:

- `/api/accounts-usage/:ytAuthCluster/check-available`, `/api/accounts-usage/:ytAuthCluster/:action` — accounts usage reports.
- `/api/:ytAuthCluster/prometheus/chart-data`, `/api/:ytAuthCluster/prometheus/discover-values` — metric charts.
- `/api/pool-names/:ytAuthCluster` — scheduler pool autocomplete.
- `/api/settings/:ytAuthCluster/:username` (GET/POST), `/api/settings/:ytAuthCluster/:username/:path` (GET/PUT/DELETE) —
  remote user settings; leaving `userSettingsConfig` unset switches the UI to localStorage and these are never called.
- `/api/strawberry/:engine/:ytAuthCluster/:action` — CHYT/SPYT clique management.
- `/api/table-column-preset/:ytAuthCluster/:hash`, `/api/table-column-preset/:ytAuthCluster[/:hash]` — shareable
  column presets; UI falls back to local settings.
- `/api/tablet-errors/:ytAuthCluster/:action` — dynamic-table error reports.

`/:ytAuthCluster/change-password/` is served by the implemented generic SPA HTML shell, but
the password self-service feature is outside the viewer (auth-none mode has no passwords).

Commands discovered by recording a wider UI session (`recordings/play-discovery.js`
visiting the Queries and Operations pages; stubs auto-generated into
`discovered.inventory.json` by `recordings/discover.py`) — both now answered
with empty-but-valid results (`support_status = constant`, see
`mock.inventory.json`) so those pages render empty states instead of
"command not registered" error blocks:

- `GET /api/v4/get_query_tracker_info` — Queries page capability probe.
- `POST /api/v3/list_operations` — Operations page listing.

Proxy token-management commands are also outside the viewer path. The UI never calls
`get_current_user` (it uses `/auth/whoami`); the other three are only used by the
token-management settings page:

- `GET|POST /api/v4/get_current_user`
- `GET /api/v4/issue_token`, `GET /api/v4/list_user_tokens`, `GET /api/v4/revoke_token`
