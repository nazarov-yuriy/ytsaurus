# Reviewer's guide to the mock backend

Line references are for commit `1c688b40130`. No code was changed for this
guide. The backend is 1,621 lines across four files; roughly 60% is faithful
reproduction of the YTsaurus HTTP proxy wire protocol, where "looks wrong" is
often "matches the real C++ proxy" — those spots are annotated with their
upstream source. Everything wire-visible is pinned by the recorded golden
corpus, so behavior changes cannot slip through silently.

## How to verify while reviewing

```bash
python3 -m pip install -r mock-backend-py/requirements.txt
python3 tests/test_protocol.py          # 58 documented-behavior tests
python3 tests/test_golden_replay.py     # 165 recorded UI requests, byte-compared
python3 tests/test_external_auth.py     # delegated auth against a fake upstream
python3 tests/test_userdb.py            # hashing, races, reconnect, audit bounds
MOCK_PG_TEST_DSN=postgresql://... python3 tests/test_user_persistence.py
python3 db/sync.py check && python3 db/sync.py audit   # docs<->DB<->code<->traffic
```

## Suggested review order

1. **`userdb.py`** — the only real state (credentials, sessions, audit). Highest
   security leverage per line.
2. **`server.py` auth section** (lines 147–248) and **login route** (546–593).
3. **`server.py` HTTP layer** (411–683) — new in the FastAPI migration.
4. **`server.py` commands** (250–392) and **`webjson.py`** — wire-format
   mechanics, golden-pinned, mostly mechanical.
5. **`data.py`** — fake data, no trust decisions; skim.

Prior review artifacts you can lean on: `docs/security-review.md` (SEC-01…06,
addressed or accepted as noted there), `docs/architecture-review.md`,
`docs/auth.md` (the protocol contract this code implements).

---

## server.py (684 lines)

### Configuration — lines 27–56

- `31` `PORT` from argv; `32` `HOST` feeds `X-YT-Proxy` and `/hosts` — the UI
  uses it for proxy discovery, so a wrong `MOCK_HOST` breaks host lists, not
  startup.
- `36–39` external auth knobs; empty `UPSTREAM` disables delegation entirely.
- `44–56` `MOCK_DELAY` slow-catalog simulation. The `//sys` exemption (line 55)
  is load-bearing: the UI server's boot-path robot batches have a 5 s timeout
  and a failing medium-list blocks the whole cluster page (`docs/timeouts.md`).

### Error envelope — lines 63–73

- `63` the 4-field YT error shape (`code/message/attributes/inner_errors`).
- `72` **looks wrong, is right**: resolve errors are YT code **500** inside an
  HTTP **400**. Code 500 is `NYTree` resolve-error, not an HTTP status. The UI
  matches on it to render "path not found" rather than an error toast.

### Header protocol — lines 76–146

- `76–95` `gather_header`: YT clients may split a header into base64 numbered
  parts (`X-YT-Error-Format0..N`). The 1000-part cap (line 87) is a DoS bound.
  Malformed → `ValueError` → HTTP 400 at the call site.
- `98–131` `parse_error_format`: the format header itself is encoded per
  `X-YT-Header-Format` — either JSON (plain string or `{$attributes,$value}`)
  or YSON text (`<annotate_with_types=%true>json`). Only
  `json|web_json|yson` are accepted (line 100).
- `134–141` the negotiated format governs **only** the `X-YT-Error` header and
  `X-YT-Error-Content-Type`; the response body stays plain JSON. That matches
  the real proxy (`context.cpp` pre-flush behavior) even though it looks
  half-finished.
- `144–146` `escape_header_value`: the fix for SEC-06 (response-header CRLF
  injection via attacker-controlled paths echoed into
  `X-YT-Response-Message`). `json.dumps` escapes every control character and
  `ensure_ascii` keeps the value latin-1-safe. h11 additionally hard-rejects
  stray CR/LF — defense in depth, not the primary defense.

### External authentication — lines 150–189

- `150–157` `RefuseRedirects`: the upstream opener raises instead of following
  redirects, so Basic credentials can never be re-sent to a redirect target
  (e.g. a compromised upstream 301-ing to an attacker host).
- `160–189` `upstream_login` truth table — review this as a security decision:
  - `True` only on 2xx **with** a `YTCypressCookie` in `Set-Cookie` (a 2xx
    without the cookie is treated as *unavailable*, not success — a proxy that
    200s everything must not become an authentication oracle);
  - `False` only on 4xx (definite rejection → masked 401);
  - `None` for everything else (5xx, timeout, network) → 503, so an outage is
    never reported to the user as "wrong password".

### CSRF — lines 192–248

- `195–199` token construction is bit-for-bit the real proxy's `SignCsrfToken`
  (`auth_server/helpers.cpp`): `hex(hmac_sha256(secret, "user:ts")) + ":" + ts`.
- `203–220` check order matters and matches `CheckCsrfToken`: malformed →
  **503** with generic code 1 (yes, 503 — real proxy parity), expired →
  401/code 110, bad signature → 401/code 110. Line 219's `'CSFR'` misspelling
  is the real proxy's typo (`helpers.cpp:187`), preserved deliberately.
- `212` the `2**53-1` bound mirrors the proxy's JS-safe-integer check.
- `241–248` only cookie-authenticated mutating requests need the token: OAuth
  and anonymous callers have no ambient browser credential to forge, so CSRF
  does not apply to them. GET/HEAD/OPTIONS exempt per the real handler.

### Trust ladder — lines 224–238 (`authenticate`)

Order: session cookie (revision-bound lookup in userdb) → `OAuth` bearer token
→ anonymous. In strict mode (`MOCK_REQUIRE_AUTH`) the only accepted token is
`MOCK_ROBOT_TOKEN` (constant-time compare, line 231) and anonymous is
rejected; in default mode everything falls back to the `iceberg` user because
`authentication: none` UIs send no credentials at all. Note the asymmetry is
intentional: cookies are always validated against the session store even in
lenient mode — a *wrong* cookie plus no token yields the anonymous fallback,
never a session hijack.

### Commands — lines 250–392

- `253` `VIRTUAL_ATTRS`: attributes the real Cypress synthesizes; served empty.
- `262–292` `cmd_get` is the subtlest command: `/@attr/inner/path` walking with
  `$value` unwrapping (line 285) and **string-indexed list access** (line 288)
  because the JS client walks arrays with string indices. Missing attribute →
  YT code 500 envelope (line 291), same shape as path resolution.
- `306–318` range parsing accepts both rich `$attributes.ranges` and the
  legacy `[#a:#b]` path suffix; `strip_ranges` before resolution.
- `322–343` `cmd_read_table`: web_json defaults (50 rows, 50 columns) match the
  UI's table widget; the `_int` guard tolerates string counts from the wire.
- `347–361` `cmd_execute_batch`: per-item `{output}|{error}` inside an HTTP
  **200** — a failing sub-command must never fail the batch. Unknown inner
  commands produce per-item errors (line 351), and per-command delays apply to
  items individually.
- `364–392` `COMMANDS`: `get/list/exists/read_table/execute_batch` are dynamic
  (backed by `data.py`, the future Iceberg surface); the rest are constants.
  `list_operations`/`get_query_tracker_info` return faithful *empty* results
  (shapes from `scheduler_commands.cpp:417-441` / `query_commands.cpp:429-437`)
  so the Operations/Queries pages render empty states instead of error blocks.
  `check_permission*` always allow — **authorization is explicitly out of
  scope** (`docs/security-review.md` SEC-01, accepted).
- `394` `RAW_OUTPUT`: `read_table` output is already wire-shaped; annotating it
  again would double-wrap.

### CORS — lines 396–408

Echo-any-origin **with credentials** (417–426). Flagged in the security review
and accepted: the mock is deployed behind the UI server on a trusted network
(ClusterIP by default), and the real UI never calls the proxy cross-origin.
Do not reuse this policy in an internet-facing deployment.

### HTTP layer (FastAPI/uvicorn) — lines 411–683

- `413` `openapi_url=None`: the YT protocol is not REST; no docs surface, and
  no accidental `/openapi.json` route.
- **Handlers are sync `def` on purpose** — command logic blocks (PostgreSQL
  under a lock, PBKDF2 at 600k iterations, `MOCK_DELAY` sleeps). Sync handlers
  run in the threadpool; an `async` handler here would freeze every concurrent
  request for the duration of a sleep. If you add a route, keep it sync unless
  it is truly non-blocking.
- `476–507` middleware = the four cross-cutting behaviors:
  1. body buffered **once** into `request.state.body_buf` (line 490; uvicorn
     has already decoded chunked transfer) — sync handlers cannot `await
     request.body()`, so any new handler must read this attribute;
  2. request log line (491);
  3. unhandled exceptions → enveloped YT 500, never a bare traceback (494–497);
  4. the **audit write** (499–505): emitted before the response object is
     returned to the transport, so the trail never lags what a client saw;
     fail-open by design (a storage outage must not take the viewer down —
     `/ready` surfaces the outage instead).
- `452–460` every response funnels through `respond()` → `record()`; that is
  what keeps `MOCK_RECORD` (the corpus re-recording mechanism) complete.
- `546–593` login: the branch ladder (no header → 401 + `WWW-Authenticate`;
  unparsable / non-Basic / bad base64 / no colon → four distinct 400s) is
  copied from `cypress_cookie_login.cpp` branch by branch. Line 584 masks
  *why* a login failed (generic code 1, "Incorrect login or password") like
  the real proxy. Lines 585–592: the external-auth block — local users are
  short-circuited before it, so a test user can never trigger an upstream
  call (pinned by `test_external_auth.py` test 5). Cookie attributes
  (line 592): `Secure; HttpOnly; Path=/`, **no SameSite** — real-proxy parity.
- `595–605` `/auth/whoami` is the UI's boot gate: it must return HTTP 200 with
  a *truthy* `csrf_token` or the whole cluster page blocks
  (`cluster-params.ts:249`). This is why even anonymous mode gets a signed
  token.
- `607–668` `api_command`: the `re.fullmatch(r'\w+')` guard (609) preserves
  the old dispatch-regex semantics exactly (e.g. `/api/v3/get/extra` and
  `/api/v2/x` fall through to the 404 catch-all). Parameter precedence is
  query string < `X-YT-Parameters` header < JSON body — `dict.update` order,
  lines 622–634 — matching the proxy. `error_format` is parsed *before*
  command execution (650–653) so a command failure can honor it. The v4
  `{value}` envelope (666) applies **only** to `get/list/exists`.
- `638–645` batch audit summaries are capped at 8 items here; the byte-level
  bound lives in `userdb._sanitize_audit` (double protection).
- `671–677` catch-all: unknown routes are YT-enveloped 404s and **audited**,
  attributed to the caller when credentials are valid — probes leave traces
  (`test_user_persistence.py` test 9b).
- `683` `host=''` binds dual-stack (v4+v6); `timeout_keep_alive=5` is the
  contract asserted by `TestConnectionManagement` — a header-less keep-alive
  response with a socket that actually stays usable.

## userdb.py (564 lines)

**Structural contract:** two complete implementations of the same 14-function
API — PostgreSQL (270–433) and in-RAM (435–549) — selected at import by
`MOCK_PG_DSN`. Any function added to one side must be added to the other;
wire behavior must be identical (the whole protocol suite passes in both
modes). RAM discipline: every public function takes `_lock`.

### Credentials — lines 17–95

- `19` seed users: `iceberg/iceberg` and `root` with an **empty password** —
  real YTsaurus local-mode parity, not an accident. Both are `origin='local'`.
- `21–22` PBKDF2-HMAC-SHA256, 600k iterations. The verify-time bounds
  (line 79: accepted range 600k–5M, 32-byte digest) mean a tampered row
  cannot downgrade hashing or DoS the server with a 10⁹-iteration hash.
- `85` `secrets.compare_digest` — constant-time.
- `93–95` cookies are 64 hex chars = `GenerateCookieValue` parity
  (`cypress_cookie.cpp:47-53`).

### Audit sanitizer — lines 24–267

Purpose: cap the user-controlled part of one audit row below 1,000 bytes of
compact JSON, without ever retaining full request bodies. Review the budget
arithmetic once: components 120 (login) + 240 (endpoint) + 600 (details)
plus the envelope leave headroom under 1,000; line 265 is the runtime
backstop if that arithmetic ever rots.

- `107–126` `_bounded_audit_text`: truncates by **encoded JSON size** (binary
  search, 119–126), not character count — multibyte and escape-heavy strings
  stay within budget. Line 112–113: invalid surrogates and U+0000 are
  replaced because PostgreSQL `jsonb` rejects ` `.
- `129–173` `_normalise_audit_value`: depth ≤ 3, ≤ 8 items per container,
  ≤ 160 bytes per string, big ints flattened; anything lossy sets a flag that
  becomes `_audit_truncated`.
- `176–192` batch `requests` entries are **allowlisted** to
  `command/path/status/error_code` — a future caller cannot accidentally
  persist full sub-request parameter payloads.
- `206–252` oversized details degrade in two stages: normalized copy, then a
  rebuilt allowlisted summary that re-adds fields (and as many batch
  summaries as fit) while staying under budget. `requests_omitted`
  bookkeeping keeps the count truthful across both truncation layers.

### Connection & session logic (PG) — lines 270–433

- `301–313` `_query`: one shared connection under an RLock; lazy reconnect
  with **one retry for reads only**. Writes pass `retry=False` — a mutation
  is never replayed after an ambiguous connection loss (a replayed
  `INSERT session` or `password_revision + 1` would be a correctness bug,
  pinned by `test_userdb.py`).
- `344–378` session creation is a **single SQL statement**: expired-session
  cleanup CTE + insert. In `authenticate_and_create_session` the insert's
  WHERE clause re-checks salt, hash *and* `password_revision` (372–373) — the
  verify-then-insert race with a concurrent password change is closed inside
  the statement (pinned by the racing-login test).
- `380–396` `session_user` joins sessions to users **on password_revision** —
  changing a password invalidates every outstanding cookie at read time, with
  no cleanup job needed.
- `328–334` `external_login`: `ON CONFLICT DO NOTHING` plus an origin
  re-check; external rows carry empty salt/hash, which `_password_matches`
  can never accept — external users have no local credential by construction,
  and a local user can never be shadowed.
- `398–404` CSRF secret: env override, else generated once and persisted in
  `settings`. (RAM mode: per-process random, line 474 — a restart invalidates
  outstanding CSRF tokens; accepted, the UI re-fetches on 401.)
- `406–418` `set_password`: upsert + `origin='local'` + revision bump +
  session purge, again one statement. This is also the "promote external user
  to local" path.

## webjson.py (167 lines)

Pure functions, no state, no trust decisions — but golden-pinned, so review
for faithfulness rather than taste.

- `7–11` `js_num_str`: JS `String(number)` parity — integer-valued floats
  print without a decimal point (`3.0` → `"3"`). Several UI code paths parse
  these strings back.
- `14–25` `annotated`: `{$attributes,$value}` wrappers pass through and
  **empty `$attributes: {}` is kept** — the UI distinguishes "empty attrs"
  from "no attrs".
- `28–47` `typed_annotate` (`annotate_with_types` + stringify): the `bool`
  check (42) must precede the `int` check (44) — `bool` is an `int` subclass
  in Python and would otherwise stringify as `int64`.
- `62–89` `_yson_quote`: byte-for-byte `NYson::EscapeC`, including the
  lookahead rules (80–87) that decide between short octal, `\xNN`, and long
  octal escapes based on the *next* byte. This is the gnarliest code in the
  repo; it is exercised by the `X-YT-Error-Format: yson` protocol tests.
- `107–127` YQL value format: cells are `[value, "<registry index>"]`,
  optionals wrap present values in a one-element list, `any` becomes
  `{"val": <typed tree>}` with type name `Yson` — names per
  `web_json_writer.cpp GetSimpleYqlTypeName`.
- `158–163` `incomplete_columns` / `incomplete_all_column_names` are
  **strings** (`"true"`/`"false"`) on the wire, not booleans.
- `130–156` `web_json_body`: `column_names` *replaces*
  `max_selected_column_count` when present; the registry deduplicates types
  by their JSON encoding.

## data.py (206 lines)

Fake in-RAM Cypress tree; the designated **Iceberg swap point** (map nodes ↔
namespaces, tables ↔ Iceberg tables). No trust decisions. One contract worth
knowing: **determinism** — node ids are sequential in creation order and all
timestamps are fixed (lines 64–92, 152), because the golden corpus compares
responses byte-for-byte. Reordering the `_insert` calls (152–171) changes ids
and breaks golden replay; that is intended behavior, regenerate with
`GOLDEN_UPDATE=1` only for deliberate changes. The `//sys` subtree is the
minimal set of nodes the UI boot path reads (media, primary_masters,
pool_trees with `@default_tree`, empty users/groups).

## Invariant → test map

| Invariant | Pinned by |
|---|---|
| Every wire response byte-identical to the recorded UI session | `test_golden_replay.py` (165 requests) |
| Login branch ladder, masked 401, cookie attributes | `test_protocol.py` login tests |
| CSRF construction/expiry/typo/503-on-malformed | `test_cookie_model.py`, `test_protocol.py` |
| Local test users never reach the external upstream | `test_external_auth.py` test 5 |
| Upstream outage ≠ wrong password (503 vs 401) | `test_external_auth.py` tests 6–7 |
| External users carry no local password material | `test_userdb.py`, `test_user_persistence.py` test 8 |
| Password change kills sessions, racing login included | `test_userdb.py` racing test, persistence test 5 |
| Interrupted PG writes are never replayed | `test_userdb.py` `TestPostgresRecovery` |
| Audit rows < 1,000 bytes, no credentials, batch allowlist | `test_userdb.py` audit tests |
| Unexpected routes/commands are audited & attributed | `test_user_persistence.py` test 9b |
| Header-less keep-alive response ⇒ genuinely reusable socket | `test_protocol.py` `TestConnectionManagement` |
| Slow requests don't block fast ones; `//sys` never delayed | `test_slow_backend.py` |
| Docs ⇄ catalog ⇄ code ⇄ recorded traffic agree | `db/sync.py check` + `audit` |

## Known, deliberate limitations (please don't re-flag)

- **No authorization.** `check_permission*` always allow; ACLs are empty. The
  viewer trusts the deployment topology (SEC-01 in `docs/security-review.md`).
- **No login rate limiting** — development mock stance, documented in README.
- **Anonymous default.** Without `MOCK_REQUIRE_AUTH` every caller is
  `iceberg`; the Helm chart enables strict mode automatically with PG/upstream
  auth and refuses the published default robot token.
- **External sessions outlive upstream revocation** until cookie TTL; local
  revocation is `userdb.py add-user` (revision bump). Documented in
  `docs/auth.md` §6.2.
- **Audit is fail-open** — availability of the viewer wins over completeness
  of the trail; `/ready` exposes the storage outage.
- **CORS echoes any origin with credentials** — safe only behind the UI tier;
  see the CORS note above.
