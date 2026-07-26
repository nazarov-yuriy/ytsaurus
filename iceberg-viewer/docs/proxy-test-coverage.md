# YTsaurus http-proxy test suites vs the mock backends

**Question:** does the YTsaurus repo have a test suite for the HTTP proxy, and how
much of it does the mock implementation cover?

**Short answer:** yes — 5 C++ unit tests plus 5 integration suites with 112 test
cases (`yt/yt/tests/integration/proxies/`). They run against a real cluster
(`YTEnvSetup` spins up masters/nodes/proxies), so they cannot be executed against
the mock directly; measured by what each case asserts, the mock's behavior
conceptually covers **~10 of 117 cases fully and ~8 partially (~9–15%)**. That is
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

**Partially covered:**

- `test_error_format` family: we mirror errors into `X-YT-Error` /
  `X-YT-Response-Code`, but **`X-YT-Error-Format` negotiation** (yson / typed
  json / web_json error bodies, `X-YT-Error-Content-Type`) is not implemented —
  the UI never sends it.
- `test_hosts` / `TestHttpProxyRoleFromStaticConfig`: we serve `/hosts`, but the
  real test asserts role filtering (`?role=control` → `[]`) and
  `//sys/http_proxies` registration — the documented `/hosts` role gap
  (architecture-review.md §3.1).
- `test_whoami_*_yt_error_header`: whoami with bad token → 401 + `X-YT-Error`
  matches in strict mode only.
- C++ CSRF test: we enforce CSRF but with a mock token scheme, not the real
  HMAC construction.

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
`tests/test_protocol.py` (43 dual-backend cases) + the 61-shape replay corpus
cover that the YT proxy suite does **not** test at all: typed
`annotate_with_types` envelopes, web_json `value_format: yql` cell/registry
encoding, `column_names` projection semantics, v4 `{value}` wrapping, virtual
attributes, `Connection:` header advertisement, the UI boot gates. So the two
suites largely test disjoint surfaces; the meaningful shared subset (basic
endpoints + cookie auth) is covered on both sides.

## 4. If more overlap is ever wanted (ordered, optional)

1. Port the remaining cookie-auth assertions (invalid-cookie rejection in
   default mode, cookie attribute shape) into `test_protocol.py`.
2. Implement role-aware `/hosts` (`?role=control` → `[]`) — also item 1 of the
   architecture-review fidelity plan — then port `test_hosts`.
3. `X-YT-Error-Format` negotiation if any non-UI SDK client will consume errors.
4. Framing (`X-YT-Accept-Framing`) only if `yt` CLI / SDK clients must work
   against the mock; the wire format is fully documented in table-viewer.md §3.7.
