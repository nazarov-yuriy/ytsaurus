# Mock YTsaurus HTTP proxy — Python implementation

The sole mock-backend implementation: protocol logic on a FastAPI/uvicorn
HTTP layer (see "HTTP layer" below), PostgreSQL optional.
Reviewing this code? Start with [REVIEW.md](REVIEW.md) — a line-anchored
walkthrough of every trust decision and wire-protocol oddity.

## Run

```bash
python3 -m pip install --requirement requirements.txt   # pinned versions
python3 server.py 8000
# then point ytsaurus-ui's clusters-config.json at localhost:8000
```

Environment:

- `MOCK_RECORD=<path>` appends sanitized request/response pairs as JSONL for
  anonymous development sessions only. Startup rejects it in strict or
  delegated-authentication mode. Credential-shaped headers, query parameters,
  and nested JSON fields are redacted; non-JSON bodies are omitted, individual
  bodies over 64 KiB are omitted, and the file stops growing at 50 MiB.
- `MOCK_PG_DSN=<libpq conninfo>` switches user/session storage to PostgreSQL.
- `MOCK_REQUIRE_AUTH=1` rejects missing/expired cookies and unknown OAuth
  tokens instead of using the anonymous `iceberg` fallback.
- `MOCK_ENABLE_DEV_SEED_USERS=1` creates the published `iceberg`/`iceberg` and
  `root`/empty users only for anonymous protocol-fidelity tests. It is ignored
  when `MOCK_REQUIRE_AUTH` is set and must not be used in a deployment.
- `MOCK_ROBOT_TOKEN=<token>` supplies the one OAuth robot token accepted in
  strict mode; it maps to the `iceberg` user.
- `MOCK_DELAY=<ms|cmd:ms,...>` simulates a slow catalog on data commands
  (`//sys` paths and infrastructure endpoints exempt) — see `../docs/timeouts.md`.
- `MOCK_CORS_ORIGINS=<origin,...>` enables credentialed browser access for an
  exact comma-separated list of `http(s)://host[:port]` origins. CORS is
  disabled by default (the normal UI server-to-backend topology does not need
  it); `null`, suffix matches, paths, userinfo, queries, and fragments are
  rejected.
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
in-RAM storage otherwise. Neither store creates password users by default;
provision local users explicitly with `userdb.py add-user`. The development
seeds described above require an explicit anonymous-test opt-in.

- Schema (auto-created on start): `users(login PK, salt, password_hash,
  origin, password_revision, created_at)` and `sessions(cookie PK, login FK,
  password_revision, created_at, expires_at)`. Passwords are stored using
  PBKDF2-HMAC-SHA256 with 600,000 iterations and 128-bit salts, never in
  plaintext. Sessions expire after the configured cookie TTL (30 days by
  default), matching the browser's `YTCypressCookie` lifetime.
- Cookies have the 64-hex shape produced by `GenerateCookieValue`, with matching
  server/browser expiry and explicit `Secure; HttpOnly; SameSite=Lax; Path=/`
  attributes. The privileged `//sys/cypress_cookies` store is not exposed
  through this authorization-light mock API. CSRF tokens use the real
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
  Authenticated mode rejects the two published development password pairs even
  if they are accidentally provisioned; stronger passwords for those login
  names and all other explicitly provisioned users continue to work.
- `/ping` reports that the process is alive; `/ready` also checks PostgreSQL and
  returns 503 while storage is unavailable.
- The PG driver ships in the always-installed `requirements.txt` (exact
  versions; the Helm chart installs the same file at pod start).
- Tests: `MOCK_PG_TEST_DSN=... python3 ../tests/test_user_persistence.py`
  (isolated-schema restart/reconnect, CLI users, password storage); the whole
  `tests/test_protocol.py` suite also passes with `MOCK_PG_DSN` set — the wire
  behavior is identical in both storage modes.
- `python3 ../tests/test_userdb.py` always runs without PostgreSQL and covers
  hash validation plus reconnect behavior with a fake driver.

## Audit log

Every user-attributable request is recorded before its response is sent —
`/login` attempts (success/rejected/upstream_unavailable, never the password),
`/auth/whoami`, and each `/api/v3|v4` command including per-item summaries of
`execute_batch`. Unexpected calls are covered too: unknown routes and
unregistered commands are audited as 404s with `error_code`, attributed to the
caller when their credentials are valid — a UI hitting something we do not
serve leaves a trace (`tests/test_user_persistence.py` test 9b). Infrastructure
endpoints (`/ping`, `/ready`, `/version`, `/hosts*`, `/api` discovery, CORS
preflights) are exempt.

The schema separates what is stable from what is not: strict columns for the
essentials — `ts timestamptz`, `login` (NULL when unauthenticated), `endpoint`
— and a schemaless `details jsonb` for the payload, whose shape is expected to
change freely (currently `method`, `status`, and per-endpoint extras such as
`command`, `path`, `requests`, `outcome`, `origin`, `error_code`). Adding a
field is just adding a dict key at the call site; no migration.

The compact JSON representation of `login`, `endpoint`, and `details` is
strictly smaller than 1,000 bytes per row. Oversized text is shortened, details
keep the high-signal fields, and large batches retain only the leading
command/path summaries plus a `requests_omitted` count; `_audit_truncated`
marks a lossy or redacted record. Nested fields whose names identify passwords,
authorization, cookies, sessions, secrets, credentials, or tokens are replaced
with `<redacted>` before either RAM or PostgreSQL persistence. Full
request/response bodies and headers are never retained by the audit log.

Storage follows `userdb.py`: the `audit_log` table in PostgreSQL (indexed by
`ts`), or a bounded in-RAM deque (last 10,000 entries) without `MOCK_PG_DSN`.
Writes are fail-open — a storage outage logs `audit write failed` and the
request is still served (`/ready` reports the outage). Inspect with
`MOCK_PG_DSN=... python3 userdb.py audit-tail [n]` (JSON lines) or SQL, e.g.
`SELECT * FROM audit_log WHERE details->>'command' = 'read_table'`.

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

## HTTP layer

The server is protocol logic on a FastAPI/uvicorn HTTP layer. It originally
ran on stdlib `http.server` to keep deployments dependency-free, which
required hand-rolled transport fixes (see "Implementation notes"); once
PostgreSQL made pinned dependencies part of the deployment anyway, the HTTP
layer was swapped for FastAPI — uvicorn owns keep-alive semantics, chunked
request decoding, listen backlog, and dual-stack binding natively. The swap
was validated against the full recorded golden corpus (165/165 byte-identical
responses) and every protocol suite. The protocol logic itself (error
envelopes, header formats, auth, commands) is framework-independent and moved
unchanged.

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

- **Wire encoding details**: integer-valued floats are stringified without a
  decimal point, and empty `$attributes` objects must be retained.
- **Transport (stdlib era, now owned by uvicorn but still asserted by tests):**
  the UI's Node/axios proxy treats a header-less HTTP/1.1 response as
  keep-alive and pools the socket — a server that then closes it silently (as
  `http.server` did for `Connection: close` requests) causes intermittent
  `socket hang up` errors and UI 504s; a UI page load bursts ~20 parallel
  connections (backlog); clients may resolve `localhost` to `::1` (dual-stack);
  axios streams proxied requests as `Transfer-Encoding: chunked`.
  `tests/test_protocol.py TestConnectionManagement` keeps these pinned.

This is still a development mock without login rate limiting, not a production
identity service.
