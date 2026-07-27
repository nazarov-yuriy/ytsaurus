# Reviewer's guide to the mock backend

This guide names functions and sections rather than line numbers so it stays
useful as the implementation moves. Much of the backend faithfully reproduces
the YTsaurus HTTP proxy wire protocol, where "looks wrong" often means "matches
the real C++ proxy"; those spots are annotated with their upstream source. The
golden corpus pins one recorded UI session, while malformed inputs,
authentication branches, concurrency, and failure paths rely on the focused
protocol and security tests below.

## How to verify while reviewing

```bash
python3 -m pip install -r mock-backend-py/requirements.txt
python3 tests/test_protocol.py
python3 tests/test_cookie_model.py
python3 tests/test_slow_backend.py
python3 tests/test_recording_security.py
python3 tests/test_golden_replay.py
python3 tests/test_external_auth.py     # delegated auth against a fake upstream
python3 tests/test_auth_configuration.py
python3 tests/test_userdb.py            # hashing, races, reconnect, audit bounds
MOCK_PG_TEST_DSN=postgresql://... python3 tests/test_user_persistence.py
bash deploy/helm/iceberg-ui-mock/tests/test-auth-render.sh
python3 db/sync.py check && python3 db/sync.py audit   # docs<->DB<->code<->traffic
```

## Suggested review order

1. **`userdb.py`** — the only real state (credentials, sessions, audit). Highest
   security leverage per line.
2. **`server.py` auth section** and **login route**.
3. **`server.py` HTTP layer**, especially its dedicated executors.
4. **`server.py` commands** and **`webjson.py`** — wire-format
   mechanics, golden-pinned, mostly mechanical.
5. **`data.py`** — fake data, no trust decisions; skim.

Prior review artifacts you can lean on: `docs/security-review.md` (a historical
review whose findings remain open unless a later commit and regression test
demonstrate otherwise), `docs/architecture-review.md`, and `docs/auth.md` (the
protocol contract this code implements).

---

## server.py

### Configuration

- `PORT` comes from argv; `HOST` feeds `X-YT-Proxy` and `/hosts` — the UI
  uses it for proxy discovery, so a wrong `MOCK_HOST` breaks host lists, not
  startup. `MOCK_BIND_HOST` is separate and defaults to `127.0.0.1`; container
  manifests explicitly bind the backend to `0.0.0.0`, while Compose exposes
  the UI only on host loopback.
- Empty `MOCK_YT_UPSTREAM` disables delegated login entirely. A non-empty
  upstream is rejected unless `MOCK_REQUIRE_AUTH=1`, preventing delegated
  verification from being placed behind anonymous fallback.
- The Helm chart requires an explicit authentication posture: configure
  PostgreSQL or delegated authentication, or deliberately set the
  development-only `auth.allowAnonymous=true`. It rejects published robot and
  database placeholder secrets when their authenticated paths are enabled,
  and rejects multiple authenticated RAM-backed replicas because their users
  and sessions would diverge.
  Direct process execution still uses anonymous fallback when
  `MOCK_REQUIRE_AUTH` is absent and must be treated as loopback development
  mode. Strict server startup also rejects the published robot placeholder,
  independently of Helm.
- `MOCK_RECORD` is rejected at startup whenever strict or delegated
  authentication is active.
- `MOCK_DELAY` provides slow-catalog simulation. The `//sys` exemption
  is load-bearing: the UI server's boot-path robot batches have a 5 s timeout
  and a failing medium-list blocks the whole cluster page (`docs/timeouts.md`).

### Error envelope

- `yt_error` builds the four-field YT shape
  (`code/message/attributes/inner_errors`).
- **Looks wrong, is right:** resolve errors are YT code **500** inside an
  HTTP **400**. Code 500 is `NYTree` resolve-error, not an HTTP status. The UI
  matches on it to render "path not found" rather than an error toast.

### Header protocol

- `gather_header`: YT clients may split a header into base64 numbered parts
  (`X-YT-Error-Format0..N`). The 1000-part cap is a DoS bound.
  Malformed → `ValueError` → HTTP 400 at the call site.
- `parse_error_format`: the format header itself is encoded per
  `X-YT-Header-Format` — either JSON (plain string or `{$attributes,$value}`)
  or YSON text (`<annotate_with_types=%true>json`). Only
  `json|web_json|yson` are accepted.
- The negotiated format governs **only** the `X-YT-Error` header and
  `X-YT-Error-Content-Type`; the response body stays plain JSON. That matches
  the real proxy (`context.cpp` pre-flush behavior) even though it looks
  half-finished.
- `escape_header_value` is the fix for SEC-06 (response-header CRLF
  injection via attacker-controlled paths echoed into
  `X-YT-Response-Message`). `json.dumps` escapes every control character and
  `ensure_ascii` keeps the value latin-1-safe. h11 additionally hard-rejects
  stray CR/LF — defense in depth, not the primary defense.

### External authentication

- `RefuseRedirects`: the upstream opener raises instead of following
  redirects, so Basic credentials can never be re-sent to a redirect target
  (e.g. a compromised upstream 301-ing to an attacker host).
- `upstream_login` has a security-sensitive truth table:

  - `True` only on 2xx **with** a `YTCypressCookie` in `Set-Cookie` (a 2xx
    without the cookie is treated as *unavailable*, not success — a proxy that
    200s everything must not become an authentication oracle);
  - `False` only on 4xx (definite rejection → masked 401);
  - `None` for everything else (5xx, timeout, network) → 503, so an outage is
    never reported to the user as "wrong password".

### CSRF

- Token construction is bit-for-bit the real proxy's `SignCsrfToken`
  (`auth_server/helpers.cpp`): `hex(hmac_sha256(secret, "user:ts")) + ":" + ts`.
- Check order matters and matches `CheckCsrfToken`: malformed →
  **503** with generic code 1 (yes, 503 — real proxy parity), expired →
  401/code 110, bad signature → 401/code 110. The `'CSFR'` misspelling is the
  real proxy's typo (`helpers.cpp:187`), preserved deliberately.
- The `2**53-1` bound mirrors the proxy's JS-safe-integer check.
- Only cookie-authenticated mutating requests need the token: OAuth
  and anonymous callers have no ambient browser credential to forge, so CSRF
  does not apply to them. GET/HEAD/OPTIONS exempt per the real handler.

### Trust ladder (`authenticate`)

Order: session cookie (revision-bound lookup in userdb) → `OAuth` bearer token
→ development-only anonymous fallback. In strict mode
(`MOCK_REQUIRE_AUTH`) the only accepted bearer token is `MOCK_ROBOT_TOKEN`
(constant-time comparison) and anonymous callers are rejected. Lenient mode
falls back to the synthetic `iceberg` identity because `authentication: none`
UIs send no credentials. Cookies are always validated against the session
store: a wrong cookie never establishes a session; it is rejected in strict
mode and reaches only the anonymous fallback in lenient mode.

### Commands

- `VIRTUAL_ATTRS` lists attributes the real Cypress synthesizes; they are
  served empty.
- `cmd_get` is the subtlest command: `/@attr/inner/path` walking includes
  `$value` unwrapping and **string-indexed list access** because the JS client
  walks arrays with string indices. A missing attribute produces the same YT
  code 500 envelope as path resolution.
- Range parsing accepts both rich `$attributes.ranges` and the
  legacy `[#a:#b]` path suffix; `strip_ranges` before resolution.
- `cmd_read_table`'s web_json defaults match the UI's table widget; the `_int`
  guard tolerates string counts from the wire.
- `cmd_execute_batch` returns per-item `{output}|{error}` inside an HTTP
  **200** — a failing sub-command must never fail the batch. Unknown inner
  commands produce per-item errors, and per-command delays apply individually.
- In `COMMANDS`, `get/list/exists/read_table/execute_batch` are dynamic
  (backed by `data.py`, the future Iceberg surface); the rest are constants.
  `list_operations`/`get_query_tracker_info` return faithful *empty* results
  (shapes from `scheduler_commands.cpp:417-441` / `query_commands.cpp:429-437`)
  so the Operations/Queries pages render empty states instead of error blocks.
  `check_permission*` always allow. That is tolerable only while the served data
  is fake and non-sensitive; server-side authorization is an unresolved
  deployment blocker before real catalog data is connected
  (`docs/security-review.md` SEC-04).
- `RAW_OUTPUT`: `read_table` output is already wire-shaped; annotating it
  again would double-wrap.

### CORS

Browser CORS is default-deny. `MOCK_CORS_ORIGINS` is a comma-separated set of
exact HTTP(S) origins; startup rejects opaque `null` origins and entries with
userinfo, paths, queries, or fragments. A response gets credentialed CORS
headers only when its `Origin` exactly matches the allowlist, and includes
`Vary: Origin`; a disallowed preflight receives 403. Keep the allowlist empty
for the normal UI reverse-proxy topology.

### HTTP layer (FastAPI/uvicorn)

- `openapi_url=None`: the YT protocol is not REST, so there is no docs surface
  or accidental `/openapi.json` route.
- Command, login, and whoami handlers are sync `def` because their work can
  block (PostgreSQL under a lock, PBKDF2, and `MOCK_DELAY`). Infrastructure
  handlers are async and route their blocking pieces through bounded,
  dedicated executors so stalled application work cannot consume the health
  path.
- `/ping` does not use FastAPI's shared sync-handler pool. `/ready` gives the
  dedicated database health check a total deadline (0.5 seconds by default)
  and returns 503 on timeout or failure. Capacity remains held until a timed-out
  worker actually exits, preventing hidden readiness queues from accumulating.
- `request_pipeline` buffers the request body once into
  `request.state.body_buf`; sync handlers must read that attribute. Logs include
  only the path, an omitted-query marker, and body length, never query values or
  request bodies. Unhandled exceptions become YT-enveloped 500 responses rather
  than bare tracebacks.
- Audit persistence uses a single dedicated worker with bounded admission and
  write deadlines. It is awaited before the response is handed to the
  transport, but is fail-open after the bound so a stalled store cannot exhaust
  the request-handler pool or build an unbounded queue. Infrastructure probes
  are exempt; `/ready` independently reports database health.
- `MOCK_RECORD` is a development-only corpus mechanism. It cannot start with
  strict or delegated authentication; it structurally redacts credential-like
  headers, query parameters, and nested JSON fields, omits opaque or oversized
  bodies, creates the file mode 0600, stops at 50 MiB, and fails open on
  recording errors.
- `login` preserves the real proxy's malformed-header branch ladder and masks
  credential rejection as generic "Incorrect login or password". Locally
  provisioned users are checked before delegated auth and never send their
  credentials upstream. Session cookies are
  `Secure; HttpOnly; SameSite=Lax; Path=/`.
- `/auth/whoami` is the UI's boot gate: it must return HTTP 200 with a truthy
  `csrf_token` or the cluster page blocks (`cluster-params.ts`).
- `api_command` preserves the old dispatch-regex semantics. Parameter
  precedence is query string < `X-YT-Parameters` header < JSON body.
  `error_format` is parsed before command execution, and the v4 `{value}`
  envelope applies only to `get/list/exists`.
- Batch audit summaries are capped by item count in `server.py`, then
  structurally redacted and byte-bounded again in `userdb._sanitize_audit`.
  Unknown routes are YT-enveloped 404s and audited, attributed to a valid
  caller when possible.
- The process binds `127.0.0.1` by default. External exposure requires an
  explicit `MOCK_BIND_HOST`; container manifests set `0.0.0.0` intentionally.
  `timeout_keep_alive=5` is pinned by `TestConnectionManagement`, including
  actual socket reuse.

## userdb.py

**Structural contract:** PostgreSQL and in-RAM implementations expose the same
public API and are selected at import by `MOCK_PG_DSN`. Any function added to
one side must be added to the other; wire behavior must be identical. RAM
discipline: every public function takes `_lock`.

### Credentials

- There are no default runtime users. The public `iceberg/iceberg` and
  `root`/empty credentials are seeded only when
  `MOCK_ENABLE_DEV_SEED_USERS=1` is explicitly set in non-strict development
  mode. Strict mode ignores that seed opt-in and rejects those published
  credential pairs even if a matching row is present.
- Local users are provisioned explicitly with `userdb.py add-user`. Passwords
  come from an interactive prompt, stdin, or a file and are not accepted in
  process argv.
- PBKDF2-HMAC-SHA256 uses 600k iterations. Verification accepts only the
  bounded 600k–5M range and a 32-byte digest, so a tampered row cannot
  downgrade hashing or force an arbitrarily expensive verification.
- Password-hash and robot-token comparisons use `secrets.compare_digest`.
- Cookies are 64 hex characters, matching `GenerateCookieValue`
  (`cypress_cookie.cpp:47-53`).

### Audit sanitizer

Purpose: cap the user-controlled part of one audit row below 1,000 bytes of
compact JSON without retaining request bodies. Component budgets leave room
for the envelope, and `_sanitize_audit` has a final runtime backstop.

- `_bounded_audit_text` truncates by **encoded JSON size**, not character
  count, so multibyte and escape-heavy strings stay within budget. Invalid
  surrogates and U+0000 are
  replaced because PostgreSQL `jsonb` rejects `U+0000`.
- `_normalise_audit_value` bounds depth, container size, string size, and
  large integers. It structurally replaces values under credential-like keys
  at any nesting level with `<redacted>`; anything lossy sets
  `_audit_truncated`.
- Batch `requests` entries are **allowlisted** to
  `command/path/status/error_code` — a future caller cannot accidentally
  persist full sub-request parameter payloads.
- Oversized details degrade in two stages: normalized copy, then a
  rebuilt allowlisted summary that re-adds fields (and as many batch
  summaries as fit) while staying under budget. `requests_omitted`
  bookkeeping keeps the count truthful across both truncation layers.

### Connection & session logic (PG)

- `_query` uses one shared connection under an RLock and reconnects lazily
  with **one retry for reads only**. Writes pass `retry=False` — a mutation
  is never replayed after an ambiguous connection loss (a replayed
  `INSERT session` or `password_revision + 1` would be a correctness bug,
  pinned by `test_userdb.py`).
- Session creation is a **single SQL statement**: expired-session
  cleanup CTE + insert. In `authenticate_and_create_session` the insert's
  WHERE clause re-checks salt, hash, and `password_revision`, so the
  verify-then-insert race with a concurrent password change is closed inside
  the statement (pinned by the racing-login test).
- `session_user` joins sessions to users **on password_revision** —
  changing a password invalidates every outstanding cookie at read time, with
  no cleanup job needed.
- `external_login` uses `ON CONFLICT DO NOTHING` plus an origin
  re-check; external rows carry empty salt/hash, which `_password_matches`
  can never accept — external users have no local credential by construction,
  and a local user can never be shadowed.
- The CSRF secret comes from an environment override or is generated once and
  persisted in `settings`. RAM mode uses a per-process random value, so a
  restart invalidates outstanding CSRF tokens and the UI re-fetches on 401.
- `set_password` performs an upsert, sets `origin='local'`, bumps the revision,
  and purges sessions in one statement. This is also the "promote external
  user to local" path.

## webjson.py

Pure functions, no state, no trust decisions — but golden-pinned, so review
for faithfulness rather than taste.

- `js_num_str`: JS `String(number)` parity — integer-valued floats
  print without a decimal point (`3.0` → `"3"`). Several UI code paths parse
  these strings back.
- `annotated`: `{$attributes,$value}` wrappers pass through and
  **empty `$attributes: {}` is kept** — the UI distinguishes "empty attrs"
  from "no attrs".
- In `typed_annotate` (`annotate_with_types` + stringify), the `bool`
  check must precede the `int` check — `bool` is an `int` subclass
  in Python and would otherwise stringify as `int64`.
- `_yson_quote`: byte-for-byte `NYson::EscapeC`, including the
  lookahead rules that decide between short octal, `\xNN`, and long
  octal escapes based on the *next* byte. This is the gnarliest code in the
  repo; it is exercised by the `X-YT-Error-Format: yson` protocol tests.
- In the YQL value format, cells are `[value, "<registry index>"]`,
  optionals wrap present values in a one-element list, `any` becomes
  `{"val": <typed tree>}` with type name `Yson` — names per
  `web_json_writer.cpp GetSimpleYqlTypeName`.
- `incomplete_columns` / `incomplete_all_column_names` are
  **strings** (`"true"`/`"false"`) on the wire, not booleans.
- In `web_json_body`, `column_names` *replaces*
  `max_selected_column_count` when present; the registry deduplicates types
  by their JSON encoding.

## data.py

Fake in-RAM Cypress tree and the current catalog-data seam (map nodes ↔
namespaces, tables ↔ Iceberg tables). The fake module makes no trust decisions;
a real catalog integration must add server-side authorization rather than
treating this as a one-file swap. One contract worth knowing is
**determinism** — node ids are sequential in creation order and all timestamps
are fixed because the golden corpus compares responses byte-for-byte.
Reordering the `_insert` calls changes ids
and breaks golden replay; that is intended behavior, regenerate with
`GOLDEN_UPDATE=1` only for deliberate changes. The `//sys` subtree is the
minimal set of nodes the UI boot path reads (media, primary_masters,
pool_trees with `@default_tree`, empty users/groups).

## Invariant → test map

| Invariant | Pinned by |
|---|---|
| Every golden response is byte-identical to the recorded UI session | `test_golden_replay.py` |
| Login branch ladder, masked 401, cookie attributes | `test_protocol.py` login tests |
| CSRF construction/expiry/typo/503-on-malformed | `test_cookie_model.py`, `test_protocol.py` |
| Strict stores have no default users or published credentials | `test_userdb.py`, `test_user_persistence.py`, `test_external_auth.py`, `test_auth_configuration.py` |
| Local users never reach the external upstream | `test_external_auth.py` |
| Upstream outage ≠ wrong password (503 vs 401) | `test_external_auth.py` |
| External users carry no local password material | `test_userdb.py`, `test_user_persistence.py` |
| Password change kills sessions, racing login included | `test_userdb.py`, `test_user_persistence.py` |
| Interrupted PG writes are never replayed | `test_userdb.py` `TestPostgresRecovery` |
| Audit rows < 1,000 bytes, no credentials, batch allowlist | `test_userdb.py` audit tests |
| Unexpected routes/commands are audited & attributed | `test_user_persistence.py` |
| Credentialed CORS is default-deny and exact-match only | `test_protocol.py` CORS tests |
| Recordings redact secrets, stay bounded, and are forbidden with auth | `test_recording_security.py` |
| Header-less keep-alive response ⇒ genuinely reusable socket | `test_protocol.py` `TestConnectionManagement` |
| Slow work cannot starve ping; readiness and audit waiters are bounded | `test_slow_backend.py` |
| Helm requires an explicit auth posture and non-placeholder secrets | Helm `tests/test-auth-render.sh` |
| Direct startup rejects unsafe auth combinations | `test_auth_configuration.py` |
| Docs ⇄ catalog ⇄ code ⇄ recorded traffic agree | `db/sync.py check` + `audit` |

## Known limitations and deployment blockers

- **No authorization.** `check_permission*` always allow and ACLs are empty.
  This must be fixed, or explicitly accepted as global read access, before
  serving data with different user entitlements (SEC-04).
- **No login rate limiting** — development mock stance, documented in README.
- **Direct-process anonymous fallback.** Without `MOCK_REQUIRE_AUTH`, a direct
  server invocation identifies unauthenticated callers as `iceberg`, but binds
  loopback unless `MOCK_BIND_HOST` explicitly exposes it. The Helm chart does
  not select this posture implicitly: anonymous deployment requires
  `auth.allowAnonymous=true`.
- **External sessions outlive upstream revocation** until cookie TTL; local
  revocation is `userdb.py add-user` (revision bump). Documented in
  `docs/auth.md` §6.2.
- **Audit is fail-open by policy.** Sanitization, dedicated bounded execution,
  and readiness protect serving, but a storage failure can still omit an audit
  row.
- **Request bodies have no application-level size limit.** Middleware buffers
  the decoded body once, so deployments must enforce a suitable limit at the
  ingress or reverse proxy until the backend owns one.
