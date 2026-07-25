# Coverage notes: conventions and out-of-scope endpoints

Companion to the generated `API-INDEX.md` / `ENTITIES.md`. Everything in the catalog
must be mentioned in a handwritten doc (`db/sync.py check` enforces it); this file
covers the two groups that don't belong in the feature docs.

## Protocol-wide conventions (apply to every `/api/v3/*` and `/api/v4/*` route)

These pseudo-entries in the catalog describe cross-cutting behavior, documented in
detail in `table-viewer.md`:

- `/api/v3/*` **error-envelope** — errors are the `yt-error` entity as the JSON body,
  mirrored into `X-YT-Error` / `X-YT-Response-Code` / `X-YT-Response-Message`; auth
  failures are 401 (codes 110/111/900), everything else 400 (or 503 in the auth layer).
- `/api/v3/*` **format-negotiation** — the request's `output_format`
  (`{"$value":"json","$attributes":{annotate_with_types, stringify}}`) governs the
  response envelope; `annotate_with_types` wraps every scalar as `{$type,$value}`.
- `/api/v3/*` **parameter-decoding** — parameter precedence: inferred formats ←
  query string ← `X-YT-Parameters` (base64 when numbered `-0/-1`) ← JSON POST body.
  The UI wrapper uses `useBodyForParameters` for all commands the viewer needs.
- `OPTIONS /api/v3/*` **CORS-preflight** — served by the proxy but irrelevant in the
  default topology (browser never talks to the proxy directly; the UI server tunnels).

## Out of scope for the Iceberg viewer (`support_status = unused`)

UI-server routes serving pages/features an Iceberg catalog viewer doesn't need.
The UI degrades gracefully when they 404/error:

- `/api/:ytAuthCluster/prometheus/chart-data`, `/api/:ytAuthCluster/prometheus/discover-values` — metric charts.
- `/api/accounts-usage/:ytAuthCluster/check-available`, `/api/accounts-usage/:ytAuthCluster/:action` — accounts usage reports.
- `/api/access-log/:ytAuthCluster/check-available`, `/api/access-log/:ytAuthCluster/:action` — access log tab
  (the check-available stub answering "not available" is enough; observed in the play corpus).
- `/api/pool-names/:ytAuthCluster` — scheduler pool autocomplete.
- `/api/settings/:ytAuthCluster/:username/:path` (GET/PUT/DELETE), `/api/settings/:ytAuthCluster/:username[/:path]` —
  remote user settings; leaving `userSettingsConfig` unset switches the UI to localStorage and these are never called.
- `/api/strawberry/:engine/:ytAuthCluster/:action` — CHYT/SPYT clique management.
- `/api/table-column-preset/:ytAuthCluster/:hash`, `/api/table-column-preset/:ytAuthCluster[/:hash]` — shareable
  column presets; UI falls back to local settings.
- `/api/tablet-errors/:ytAuthCluster/:action` — dynamic-table error reports.
- `/:ytAuthCluster/change-password/` — password self-service page (auth-none mode has no passwords).

Proxy token-management commands, likewise unused (UI only calls them from the
token-management settings page):

- `GET|POST /api/v4/get_current_user`
- `GET /api/v4/issue_token`, `GET /api/v4/list_user_tokens`, `GET /api/v4/revoke_token`
