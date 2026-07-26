# Mock YTsaurus HTTP proxy — Python implementation

Python (stdlib-only) port of `../mock-backend/` (Node), byte-compatible on the wire.

## Run

```bash
python3 server.py 8000
# then point ytsaurus-ui's clusters-config.json at localhost:8000, same as the Node mock
```

Environment:

- `MOCK_RECORD=<path>` appends request/response JSONL (same format as Node).
- `MOCK_PG_DSN=<libpq conninfo>` switches user/session storage to PostgreSQL.
- `MOCK_REQUIRE_AUTH=1` rejects missing/expired cookies and unknown OAuth
  tokens instead of using the anonymous `iceberg` fallback.
- `MOCK_ROBOT_TOKEN=<token>` supplies the one OAuth robot token accepted in
  strict mode; it maps to the `iceberg` user.
- `MOCK_DELAY=<ms|cmd:ms,...>` simulates a slow catalog on data commands
  (`//sys` paths and infrastructure endpoints exempt) — see `../docs/timeouts.md`.

## User management (PostgreSQL)

Users and login sessions are the one piece of real state; table data stays fake.
`userdb.py` speaks PostgreSQL when `MOCK_PG_DSN` is set and falls back to
in-RAM storage (seed users `iceberg`/`iceberg`, `root`/empty) otherwise — the
Node backend and all parity tests run in the fallback mode.

- Schema (auto-created on start): `users(login PK, salt, password_hash, created_at)`
  and `sessions(cookie PK, login FK, created_at, expires_at)`. Passwords are
  stored using PBKDF2-HMAC-SHA256 with 600,000 iterations and 128-bit salts,
  never in plaintext. Existing salted-SHA256 rows remain valid and are upgraded
  after a successful login. Sessions expire after 30 days, matching the
  `YTCypressCookie` lifetime.
- With PostgreSQL, password logins and their cookies survive server restarts,
  connection loss is recovered lazily, and users added out-of-band are visible
  without a restart:
  `MOCK_PG_DSN=... python3 userdb.py add-user <login> <password>` (also `list-users`).
- `/ping` reports that the process is alive; `/ready` also checks PostgreSQL and
  returns 503 while storage is unavailable.
- Requires `psycopg` (`pip install "psycopg[binary]"`) only in PG mode.
- Tests: `MOCK_PG_TEST_DSN=... python3 ../tests/test_user_persistence.py`
  (isolated-schema restart/reconnect, CLI users, hash migration); the whole
  `tests/test_protocol.py` suite also passes with `MOCK_PG_DSN` set — the wire
  behavior is identical in both storage modes.
- `python3 ../tests/test_userdb.py` always runs without PostgreSQL and covers
  hash parsing/migration plus reconnect behavior with a fake driver.

## Files (1:1 with the Node implementation)

- `server.py` ← `server.js` — routing, auth (Basic `/login`, `YTCypressCookie`
  sessions, CSRF, optional strict credentials and robot token), command
  dispatch, error envelopes, v4 `{value}` wrapping, typed annotation.
- `data.py` ← `data.js` — the in-RAM cluster. Node-id sequence, timestamps, and
  generated rows are identical to the Node version. **Swap this file for an
  Apache Iceberg catalog implementation; everything else stays.**
- `webjson.py` ← `webjson.js` — annotated JSON, typed annotation, web_json (schemaless and yql value formats).
  Includes JS-compatible number stringification (`3`, not `3.0`).

## Consistency guarantees

Verified equivalent to the Node backend by:

1. `../recordings/replay-diff.py` — replays all 165 recorded UI requests plus 26
   edge cases against both servers side by side and diffs status, body, and YT
   headers: **191/191 identical**.
2. `../tests/test_protocol.py` — 49 documented-behavior conformance tests run
   against both backends.
3. Headless-Chromium runs of the real UI against this server: repeated runs with
   zero request failures and zero page errors.

## Porting gotchas (why some code looks the way it does)

Found the hard way while making the UI run cleanly on this server:

- **Connection headers must be explicit.** Python's `http.server` closes
  `Connection: close` requests silently; Node clients treat a header-less
  HTTP/1.1 response as keep-alive and pool the dying socket → intermittent
  `socket hang up` → 504s in the UI. `send_body` therefore always sends
  `Connection: close|keep-alive` (+ `Keep-Alive: timeout=5`, enforced with a
  socket timeout), matching Node's behavior.
- **Listen backlog**: `http.server` defaults to 5; a UI page load bursts ~20
  parallel connections. `request_queue_size = 511` matches Node.
- **Dual-stack bind**: Node's `listen()` accepts IPv4 and IPv6; Python defaults
  to IPv4-only, and clients resolving `localhost` to `::1` would fail.
- **Chunked request bodies**: axios streams proxied requests with
  `Transfer-Encoding: chunked`, which `BaseHTTPRequestHandler` does not decode.
- **JS semantics in encoders**: `String(3.0) === "3"`, and an empty `$attributes`
  object is truthy in JS (kept on the wire) but falsy in Python.

This is still a development mock without login rate limiting, not a production
identity service.
