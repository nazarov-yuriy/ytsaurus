# YTsaurus http-proxy test suites vs the mock backends

**Question:** does the YTsaurus repo have a test suite for the HTTP proxy, and how
much of it does the mock implementation cover?

**Short answer:** yes — 5 C++ unit tests plus 5 integration suites with 112 test
cases (`yt/yt/tests/integration/proxies/`). They run against a real cluster
(`YTEnvSetup` spins up masters/nodes/proxies), so they cannot be executed against
the mock directly; measured by what each case asserts, the mock's behavior
conceptually covers **~16 of 117 cases fully, plus 2 residual partials (~15%)**
(after the fixes below — originally ~10 full / ~8 partial). That is
expected, not alarming: the suites overwhelmingly test proxy *infrastructure*
(framing, compression, memory limits, metrics, roles, token management) that is
explicitly out of the viewer's scope, while the UI-facing protocol surface the
mock implements is tested far more deeply by our own suites than by YT's.

## 1. What exists in the YTsaurus repo

| Suite | Cases | Focus |
|---|---|---|
| `yt/yt/server/http_proxy/unittests/` (C++) | 5 | framing stream, query parsing, CSRF token crypto, user-agent detection, secret masking |
| `tests/integration/proxies/test_http_proxy.py` | 72 | endpoints, errors, framing, formats, memory limits, metrics, snapshots, signatures, discovery |
| `tests/integration/proxies/test_cypress_cookie_auth.py` | 9 | the `/login` + `YTCypressCookie` flow |
| `tests/integration/proxies/test_cypress_token_auth.py` | 13 | issue/revoke/list tokens, whoami headers |
| `tests/integration/proxies/test_proxy_roles.py` | 4 | role registration and `/hosts` filtering |
| `tests/integration/proxies/test_oauth.py` | 14 | proxy-side OAuth/ACO |
| *(related, different proxy type)* `test_rpc_proxy.py`, `test_grpc_proxy.py`, `tests/cpp/test_multiproxy` | — | not applicable to the HTTP mock |

## 2. Coverage map

**Fully covered by the mock (assertion-level match):**

- `test_ping`, `test_version`, `test_supported_api_versions` (basic endpoints).
- Cookie-auth core — the closest overlap, and it exists *because* review work
  ported the real branches: `test_login_401` (missing Authorization → 401 +
  `WWW-Authenticate: Basic`), `test_login_failed` (masked generic error),
  `test_weird_password` (base64 credential decoding), `test_request_with_cookie`;
  `test_request_with_invalid_cookie` in `MOCK_REQUIRE_AUTH=1` mode.
- C++ `TTestParseQueryTest` conceptually (query-string parameter source).
- `test_error_format` / `test_error_format_type` / `test_error_web_json`:
  **`X-YT-Error-Format` negotiation implemented** — `<format=text>yson` returns
  YSON text with `X-YT-Error-Content-Type: application/x-yt-yson-text`,
  `<annotate_with_types=%true>json` returns typed scalars, default/web_json
  plain JSON; assertions ported into `TestErrorFormatNegotiation`.
- `test_hosts` role filtering / `test_proxy_roles.test_simple`: **`/hosts` is
  now role-aware** (`?role=data`/default → `[self]`, other roles → `[]`,
  matching `coordinator.cpp` with this mock as one data-role proxy); ported
  into `TestHostsRoleFiltering`. `//sys/http_proxies` registration itself
  remains out of scope.
- `test_whoami_invalid_token_yt_error_header` /
  `test_whoami_valid_token_no_yt_error_header`: ported into `TestStrictAuth`
  (strict mode; 401 carries `X-YT-Error`, 200 does not).

**Residual partials (accepted):**

- C++ CSRF test: we enforce CSRF but with a mock token scheme, not the real
  HMAC construction (needs the cluster keystore).
- Cookie storage-model cases (`test_cookie_in_cypress`, `test_cookie_format`,
  rotation): ours is a PostgreSQL table by design, not `//sys/cypress_cookies`.

**Not covered — and deliberately so** (~90 cases): framing + keep-alive frames and
compressed `read_table` over framing (UI never negotiates framing), YSON
`X-YT-Header-Format` parameter encoding (UI uses JSON bodies), transactions and
pinger, memory-pressure drops, Solomon/heap metrics, structured logging, access
checker, per-user format config, build-snapshot, request signatures, dynamic
config, discover_versions, token issue/revoke/list, proxy-side OAuth, role ACLs,
job-shell audit.

## 3. The inverse view (why the % is not the real story)

YT's integration suite asserts infrastructure properties of the *implementation*;
it barely touches what the UI actually depends on. Behaviors our
`tests/test_protocol.py` (49 dual-backend cases) + the 61-shape replay corpus
cover that the YT proxy suite does **not** test at all: typed
`annotate_with_types` envelopes, web_json `value_format: yql` cell/registry
encoding, `column_names` projection semantics, v4 `{value}` wrapping, virtual
attributes, `Connection:` header advertisement, the UI boot gates. So the two
suites largely test disjoint surfaces; the meaningful shared subset (basic
endpoints + cookie auth) is covered on both sides.

## 4. If more overlap is ever wanted (ordered, optional)

~~Role-aware `/hosts`~~ and ~~`X-YT-Error-Format` negotiation~~ are done (see §2).
Remaining candidates:

1. Cookie attribute-shape assertions (`test_cookie_format`) if the PG session
   store ever mirrors the Cypress cookie model.
2. Framing (`X-YT-Accept-Framing`) only if `yt` CLI / SDK clients must work
   against the mock; the wire format is fully documented in table-viewer.md §3.7.
