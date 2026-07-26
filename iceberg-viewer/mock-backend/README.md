# Mock YTsaurus HTTP proxy (in-RAM data)

A single-file Node HTTP server that speaks enough of the YT HTTP-proxy protocol for
ytsaurus-ui to log in, browse the navigation tree, and display static tables.
No dependencies.

## Run

```bash
# 1. mock proxy
node server.js 8000

# 2. UI (once): clusters-config.json already points "mock" at localhost:8000
cd ../ytsaurus-ui/packages/ui
npm ci                    # first time only
LOCAL_DEV_PORT=8080 npm run dev:app

# 3. open http://localhost:8080/mock/navigation?path=//home/iceberg/warehouse
```

`authentication: "none"` in clusters-config.json means no login form; the mock treats
credential-less requests as user `iceberg`. Password login also works (`/login` with
HTTP Basic; users in `data.js`: iceberg/iceberg).

Set `MOCK_REQUIRE_AUTH=1` to reject missing/expired credentials and set
`MOCK_ROBOT_TOKEN=<token>` for the OAuth robot token accepted in that mode.
`MOCK_COOKIE_TTL_SECONDS` controls both session and browser-cookie expiry
(30 days by default); `MOCK_CSRF_SECRET` and `MOCK_CSRF_TTL_SECONDS` control
signed CSRF tokens. Cookies are `Secure`, so local HTTP password-auth testing
requires the UI's `ytAuthAllowInsecure` option.

## Files

- `server.js` — routing, auth (cookie + CSRF + anonymous), command dispatch,
  YT error envelopes (`X-YT-Error`, 401 auth / 400 command), v4 `{value: ...}` wrapping,
  `TYPED_OUTPUT_FORMAT` annotation. Unknown routes/commands are logged with `!!`.
- `data.js` — the in-RAM "cluster": Cypress-like tree, table schemas + rows, users.
  **This is the only file to reimplement over an Apache Iceberg catalog**
  (namespaces → map nodes, Iceberg tables → table nodes, snapshots/manifests → rows).
- `webjson.js` — encoders: annotated JSON, typed-annotated JSON
  (`annotate_with_types`), and `web_json` (table rows).

## Implemented surface

Infra: `/ping`, `/ready`, `/version`, `/hosts`, `/hosts/all`, `/api`, `/login`,
`/auth/whoami`.
Commands (v3+v4): `get`, `list`, `exists`, `read_table` (web_json + ranges +
column_names), `execute_batch`, `check_permission`, `check_permission_by_acl`,
`get_supported_features`, `get_table_columnar_statistics`, `whoami`.

See `../docs/` for the protocol details and `../docs/empirical-findings.md` for the
gotchas that only surfaced when driving the real UI.
