# YTsaurus http-proxy test suites vs the mock backends

**Question:** does the YTsaurus repository have HTTP-proxy tests, and how much
of their behavior do the mock backends reproduce?

**Short answer:** yes. In the reviewed tree there are five C++ `TEST` bodies and
112 Python `def test_*` definitions across five integration files. The Python
number is a source inventory, not a count of collected executions:
parameterization can multiply a definition, inheritance can collect it in more
than one class, and skip markers can remove it. It is therefore not a sound
denominator for a coverage percentage.

The integration suites require a real `YTEnvSetup` cluster, so they cannot be
pointed at these mocks directly. The useful comparison is assertion-by-assertion:
the small UI-facing overlap is mostly present, while cluster and proxy
infrastructure is intentionally absent.

## 1. What exists in the YTsaurus repository

| Suite | Source definitions | Focus |
|---|---:|---|
| `yt/yt/server/http_proxy/unittests/` (C++) | 5 | framing stream, structured query parsing, CSRF crypto, user-agent detection, secret masking |
| `tests/integration/proxies/test_http_proxy.py` | 72 | endpoints, errors, framing, formats, memory limits, metrics, snapshots, signatures, discovery |
| `tests/integration/proxies/test_cypress_cookie_auth.py` | 9 | `/login`, `YTCypressCookie`, Cypress storage and renewal |
| `tests/integration/proxies/test_cypress_token_auth.py` | 13 | token lifecycle and whoami headers |
| `tests/integration/proxies/test_proxy_roles.py` | 4 | role-map CRUD and ACLs; it does not test `/hosts` |
| `tests/integration/proxies/test_oauth.py` | 14 | proxy-side OAuth/ACO |
| Related RPC/GRPC proxy suites | — | different proxy types; not applicable to these mocks |

## 2. Assertion-level coverage map

Direct local matches:

- Basic endpoint assertions from `test_ping`, `test_version`, and
  `test_supported_api_versions`.
- The core login branches: `/login`, `/login/`, and nested login paths; the
  empty Basic challenge when authorization is missing; masked unknown-user and
  wrong-password failures; cookie authentication; and rejection of invalid or
  expired cookies in strict mode.
- The cookie value and response-header assertions: 64 lowercase hex characters,
  configured `Expires`, `Secure`, `HttpOnly`, and `Path=/`. Python sessions are
  also revision-bound so password changes, including a racing old-password
  login, cannot leave a valid session.
- C++ `TTestCsrfTokenTest` and the cookie-auth CSRF branches: the real
  `hex(hmac_sha256(secret, "user:timestamp")) + ":" + timestamp` construction,
  plus distinct missing, malformed, expired, and invalid-signature errors.
- The core `X-YT-Error-Format` behavior: the requested JSON/YSON/web_json
  encoding applies to `X-YT-Error`, YSON content type is advertised, and the
  response body remains ordinary JSON. Structured and multipart format headers
  have local regression coverage.
- `test_whoami_invalid_token_yt_error_header` and
  `test_whoami_valid_token_no_yt_error_header`: strict-mode failures carry
  `X-YT-Error`, successful fixed robot-token authentication does not.
- The v4 payload envelope asserted by `TestHttpProxyFraming.test_get`. The local
  protocol suite checks `{value: ...}` independently of framing.

Partial overlaps:

- C++ `TTestParseQueryTest`: the mocks accept a flat query-string parameter
  source and define source precedence, but do not build nested maps/lists from
  bracket notation or reject conflicting container shapes.
- `test_http_proxy.py::test_hosts`: the mocks reproduce the static one-data-proxy
  result (`default`/`role=data` returns self; another role returns `[]`), but not
  live proxy registration, custom role changes, or the associated metrics
  assertion. `test_proxy_roles.py` covers role-map CRUD/ACL behavior instead and
  has no `/hosts` assertion.
- `test_error_web_json`: the small-integer branch is covered; there is no local
  error fixture exercising the large-integer typed representation used for a
  real transaction id.
- The Cypress-cookie suite: ordinary login/use/expiry behavior is represented,
  but arbitrary user lifecycle is only available in the Python store. The Node
  fixture has static users, so the upstream weird-password and password-change
  scenarios are not full dual-backend matches.
- The framing `test_get` payload matches after decoding, but the framing and
  keep-alive transport surrounding that payload is not implemented.

Intentionally not reproduced:

- `test_cookie_in_cypress`: exposing `//sys/cypress_cookies` through a mock with
  no Cypress ACL engine would disclose raw bearer cookies.
- `test_cookie_rotation` and `test_periodic_cookie_fetch`: the UI tunnel cannot
  propagate a renewed upstream cookie into its cluster-prefixed browser cookie,
  so renewal would create sessions without updating the credential the UI uses.
- Framing/compressed streaming, transactions and pingers, memory-pressure
  behavior, Solomon/heap metrics, structured logging, access checking,
  per-user format configuration, snapshot building, request signatures,
  dynamic proxy discovery, token issue/revoke/list, OAuth/ACO, role-map ACLs,
  job-shell audit, user-agent detection, and secret-parameter masking.

## 3. Why a single percentage would mislead

The local protocol suite runs the same assertions against both backends, and the
replay corpus adds 61 recorded UI request shapes. Those tests go deeper on the
viewer contract: typed output envelopes, web_json `value_format: yql`,
`column_names` projection, virtual attributes, connection handling, and UI boot
gates. Upstream goes much deeper on production-proxy infrastructure.

The surfaces are substantially different. In particular, v4 `{value}` wrapping
must not be listed as local-only: upstream asserts it in the framing suite. A
definition-based fraction would hide these distinctions rather than summarize
them.

## 4. Optional ways to increase overlap

1. Implement nested bracket-query parsing if URL-parameter SDK clients become a
   supported caller.
2. Add a large-integer `web_json` error fixture.
3. Add dynamic proxy roles, `/hosts/all`, and metrics only if system/operations
   views need realistic proxy inventory.
4. Implement `X-YT-Accept-Framing` only if CLI/SDK clients must use the mock; the
   wire format is documented in `table-viewer.md` §3.7.

The privileged Cypress cookie store and ineffective UI-path renewal should
remain excluded for the security and propagation reasons above.
