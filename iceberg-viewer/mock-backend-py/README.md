# Mock YTsaurus HTTP proxy — Python implementation

The sole mock-backend implementation. It uses only the Python standard library
unless PostgreSQL-backed user and session storage is enabled.

## Run

```bash
python3 server.py 8000
# then point ytsaurus-ui's clusters-config.json at localhost:8000
```

Environment:

- `MOCK_RECORD=<path>` appends request/response pairs as JSONL.
- `MOCK_PG_DSN=<libpq conninfo>` switches user/session storage to PostgreSQL.
- `MOCK_REQUIRE_AUTH=1` rejects missing/expired cookies and unknown OAuth
  tokens instead of using the anonymous `iceberg` fallback.
- `MOCK_ROBOT_TOKEN=<token>` supplies the one OAuth robot token accepted in
  strict mode; it maps to the `iceberg` user.
- `MOCK_DELAY=<ms|cmd:ms,...>` simulates a slow catalog on data commands
  (`//sys` paths and infrastructure endpoints exempt) — see `../docs/timeouts.md`.
- `MOCK_CSRF_SECRET`, `MOCK_CSRF_TTL_SECONDS` (default 86400) — CSRF HMAC secret
  and token validity; without the env the secret is persisted in PostgreSQL
  (`settings` table) or random per process in RAM mode.
- `MOCK_COOKIE_TTL_SECONDS` (default 30d) — server and browser-cookie lifetime.
  Cookies are `Secure`; local HTTP password-auth testing requires the UI's
  `ytAuthAllowInsecure` option.
- `MOCK_YT_UPSTREAM=<real proxy URL>` delegates identity of users not added
  locally to a real YTsaurus `/login`; verified users are provisioned into the
  local store (`origin='external'`, no password material) on first success.
  Locally-added users always authenticate locally and never contact the
  upstream. `MOCK_YT_UPSTREAM_TIMEOUT` (default 5 s) bounds each verification;
  upstream failures surface as 503, not 401. See docs/auth.md §6.

## User management (PostgreSQL)

Users and login sessions are the one piece of real state; table data stays fake.
`userdb.py` speaks PostgreSQL when `MOCK_PG_DSN` is set and falls back to
in-RAM storage (seed users `iceberg`/`iceberg`, `root`/empty) otherwise.

- Schema (auto-created on start): `users(login PK, salt, password_hash,
  password_revision, created_at)` and `sessions(cookie PK, login FK,
  password_revision, created_at, expires_at)`. Passwords are stored using
  PBKDF2-HMAC-SHA256 with 600,000 iterations and 128-bit salts, never in
  plaintext. Sessions expire after the configured cookie TTL (30 days by
  default), matching the browser's `YTCypressCookie` lifetime.
- Cookies have the 64-hex shape produced by `GenerateCookieValue`, with matching
  server/browser expiry. The privileged `//sys/cypress_cookies` store is not
  exposed through this authorization-light mock API. CSRF tokens use the real
  SignCsrfToken HMAC construction (`tests/test_cookie_model.py`).
- Password changes revoke the user's existing sessions and increment a revision
  checked during authentication, so a racing login with the old password cannot
  leave a valid session. The backend does not emulate near-expiry cookie renewal
  because the UI tunnel does not propagate a renewed proxy cookie into its
  cluster-prefixed authentication cookie.
- With PostgreSQL, password logins and their cookies survive server restarts,
  connection loss is recovered lazily, and users added out-of-band are visible
  without a restart:
  `MOCK_PG_DSN=... python3 userdb.py add-user <login> <password>` (also `list-users`).
- `/ping` reports that the process is alive; `/ready` also checks PostgreSQL and
  returns 503 while storage is unavailable.
- PG mode requires the exact dependency versions in `requirements.txt`; install
  them with `python3 -m pip install --requirement requirements.txt`.
- Tests: `MOCK_PG_TEST_DSN=... python3 ../tests/test_user_persistence.py`
  (isolated-schema restart/reconnect, CLI users, password storage); the whole
  `tests/test_protocol.py` suite also passes with `MOCK_PG_DSN` set — the wire
  behavior is identical in both storage modes.
- `python3 ../tests/test_userdb.py` always runs without PostgreSQL and covers
  hash validation plus reconnect behavior with a fake driver.

## Files

- `server.py` — routing, auth (Basic `/login`, `YTCypressCookie` sessions,
  CSRF, optional strict credentials and robot token), command dispatch, error
  envelopes, v4 `{value}` wrapping, and typed annotation.
- `data.py` — the deterministic in-RAM cluster. **Swap this file for an Apache
  Iceberg catalog implementation; everything else stays.**
- `userdb.py` — in-RAM or PostgreSQL-backed users, password hashes, sessions,
  and the CSRF secret.
- `webjson.py` — annotated JSON, typed annotation, and `web_json` encoders
  (schemaless and YQL value formats).

## Validation

The backend is covered by:

1. `../tests/test_protocol.py` — documented protocol behavior in normal and
   strict-auth modes.
2. `../tests/test_cookie_model.py` and `../tests/test_slow_backend.py` — cookie,
   CSRF, delay, concurrency, and infrastructure-path behavior.
3. Headless-Chromium runs of the real UI against this server, with repeated runs
   showing zero request failures and zero page errors.

## Implementation notes

Found the hard way while making the UI run cleanly on this server:

- **Connection headers must be explicit.** Python's `http.server` closes
  `Connection: close` requests silently; the UI's Node/axios proxy treats a
  header-less HTTP/1.1 response as keep-alive and pools the dying socket,
  causing intermittent `socket hang up` errors and UI 504s. `send_body`
  therefore always sends
  `Connection: close|keep-alive` (+ `Keep-Alive: timeout=5`, enforced with a
  socket timeout).
- **Listen backlog**: `http.server` defaults to 5; a UI page load bursts ~20
  parallel connections, so `request_queue_size` is raised to 511.
- **Dual-stack bind**: Python defaults to IPv4-only, while clients may resolve
  `localhost` to `::1`.
- **Chunked request bodies**: axios streams proxied requests with
  `Transfer-Encoding: chunked`, which `BaseHTTPRequestHandler` does not decode.
- **Wire encoding details**: integer-valued floats are stringified without a
  decimal point, and empty `$attributes` objects must be retained.

This is still a development mock without login rate limiting, not a production
identity service.
