# Backend security review

Review date: 2026-07-26

Reviewed revision: `d82922e8a44`

Status: findings only; no executable code was changed as part of this review.

## Scope and threat model

The primary target is the Python backend deployed by
`deploy/helm/iceberg-ui-mock`, including its PostgreSQL authentication store,
the UI-to-backend proxy hop, and the Helm/Docker security boundary. The
ConfigMap copies under `deploy/helm/iceberg-ui-mock/files/` were byte-identical
to `mock-backend-py/` at review time. The Node backend was checked for
security-relevant protocol differences, but it is not the implementation
deployed by the chart.

The review assumes:

- ordinary internal users can reach the UI;
- a malicious or compromised workload may be able to reach ClusterIP Services;
- internal users are not trusted with other users' sessions or unrestricted
  catalog access;
- the current fake/read-only data will eventually be replaced by data whose
  confidentiality matters; and
- an attacker does not start with Kubernetes administrator or host access.

The current chart describes itself as a development mock. That explains some
of the choices below, but it also means the current chart is not a safe
internal deployment profile.

## Overall result

**Do not expose the current deployment to internal users yet.** Authentication
is disabled by default. Enabling the supplied authenticated mode does not
close the boundary because the application, robot, and PostgreSQL credentials
are published defaults. After those blockers, the service still needs
authorization enforcement, network/TLS isolation, browser-origin controls,
and request resource limits.

### Finding summary

| ID | Severity | Finding |
|---|---|---|
| SEC-01 | Critical | The default Helm deployment is anonymous and the backend fails open |
| SEC-02 | Critical | Strict authentication accepts published user and robot credentials |
| SEC-03 | Critical | PostgreSQL exposes a published bootstrap-superuser credential |
| SEC-04 | High | Authentication does not enforce any catalog/object authorization |
| SEC-05 | High | Arbitrary credentialed CORS and predictable CSRF defeat the browser boundary |
| SEC-06 | High | Attacker-controlled error text causes HTTP response-header injection |
| SEC-07 | High | Passwords, robot tokens, and sessions travel over plaintext HTTP |
| SEC-08 | High | Unauthenticated clients can exhaust threads, memory, CPU, and request time |
| SEC-09 | High | There is no network isolation between users/workloads, backend, and PostgreSQL |
| SEC-10 | Medium | Sessions cannot be reliably expired or revoked |
| SEC-11 | Medium | The hand-written HTTP framing accepts ambiguous or malformed requests |
| SEC-12 | Medium | Verb and command dispatch do not enforce a safe operation policy |
| SEC-13 | Medium | Recording and administration workflows expose reusable credentials |
| SEC-14 | Medium | The default backend workload is root and receives an unnecessary API token |
| SEC-15 | Medium | Startup executes mutable, unpinned third-party code |
| SEC-16 | Medium | Direct-run defaults bind to every IPv4 and IPv6 interface |

## Findings

### SEC-01 — The default Helm deployment is anonymous and the backend fails open

**Severity: Critical — deployment blocker**

PostgreSQL is disabled and cluster authentication is empty in
[`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L24-L28) and
[`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L62-L71).
The helper resolves that combination to `authentication: none`
([`_helpers.tpl`](../deploy/helm/iceberg-ui-mock/templates/_helpers.tpl#L80-L92)),
so the Deployment omits `MOCK_REQUIRE_AUTH`
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L64-L72)).
The backend then maps a request with no credential to the `iceberg` identity
([`server.py`](../mock-backend-py/server.py#L73-L87)).

`helm template` with no overrides confirmed that the rendered cluster uses
`authentication: "none"` and that the backend container has no authentication
gate. Any caller able to reach the UI tunnel or backend Service can issue API
commands without authenticating.

**Required change:** fail closed by default. Authentication enforcement must
not depend on enabling optional persistence. Anonymous mode should require an
explicit, conspicuously named development-only opt-in, and Helm should reject
an exposed release without a configured authentication mechanism.

### SEC-02 — Strict authentication accepts published user and robot credentials

**Severity: Critical — deployment blocker**

The user store always seeds `iceberg` with password `iceberg` and `root` with
an empty password
([`userdb.py`](../mock-backend-py/userdb.py#L14-L19)). PostgreSQL connection
initialization inserts those users again whenever they are missing
([`userdb.py`](../mock-backend-py/userdb.py#L89-L98)), so deleting a seed
account is not durable across a later reconnect. The chart also publishes
`mock-robot-token` as the accepted bearer token
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L48-L51)), and
strict mode accepts that token directly
([`server.py`](../mock-backend-py/server.py#L78-L83)).

A live strict-mode check confirmed that
`Authorization: Basic cm9vdDo=` (`root:`) returns `200` and creates a session.
`iceberg:iceberg` and `Authorization: OAuth mock-robot-token` are equivalent
published entry paths. The `root` identity has no extra privilege in the
current mock, but the request has fully crossed the authentication boundary.

**Required change:** remove runtime seed accounts, bootstrap an initial
administrator once from a required high-entropy Secret, never recreate deleted
accounts, and require an external/generated robot token while rejecting the
documented default. Existing deployments must rotate all three credentials and
revoke sessions created with them.

### SEC-03 — PostgreSQL exposes a published bootstrap-superuser credential

**Severity: Critical — deployment blocker when PostgreSQL is enabled**

The chart publishes database `mockusers`, role `mock`, and password
`mock-password`
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L27-L40)).
That role is passed as `POSTGRES_USER` to the official image
([`postgres.yaml`](../deploy/helm/iceberg-ui-mock/templates/postgres.yaml#L57-L66)).
For a newly initialized official PostgreSQL container, this is the bootstrap
database superuser. The backend uses the same role
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L73-L88)),
and PostgreSQL is exposed through a namespace/cluster-reachable Service
([`postgres.yaml`](../deploy/helm/iceberg-ui-mock/templates/postgres.yaml#L112-L126)).

A workload that can reach that Service can use the published password to read
live bearer-session cookies, replace password hashes, insert sessions, or use
PostgreSQL superuser facilities in the database container. Changing only the
UI password or robot token does not close this path. Even after rotating the
database password, a backend compromise still obtains a database superuser
credential.

**Required change:** reject the default database password, isolate PostgreSQL
at the network layer, and separate a migration/owner identity from a
least-privilege runtime role limited to the required user and session
operations. Keep administrator credentials out of the backend pod.

### SEC-04 — Authentication does not enforce any catalog/object authorization

**Severity: High when users have different data entitlements**

`get`, `list`, and `read_table` receive the authenticated identity but never
use it to authorize the requested path
([`server.py`](../mock-backend-py/server.py#L107-L188)). Dispatch checks only
that some identity exists
([`server.py`](../mock-backend-py/server.py#L405-L430)).
`check_permission` and `check_permission_by_acl` always report `allow`
([`server.py`](../mock-backend-py/server.py#L208-L225)).

Consequently every password user and the robot can enumerate and read every
path the service identity can access. A UI permission result is advisory and
cannot replace enforcement in the command handler. This is currently less
visible because data is fake and commands are read-only, but it becomes a
direct confidentiality boundary when a real Iceberg catalog is attached.

**Required change:** define the intended authorization model before attaching
real data. Enforce it server-side, deny by default before resolving or reading
a path, scope the catalog service identity to the minimum data it may expose,
and make permission-reporting commands reflect the same policy. If all
authenticated users intentionally receive global read access, document and
test that as an explicit security decision.

### SEC-05 — Arbitrary credentialed CORS and predictable CSRF defeat the browser boundary

**Severity: High when a browser can reach the backend**

The backend reflects every `Origin` value while returning
`Access-Control-Allow-Credentials: true` and allowing authorization and CSRF
headers
([`server.py`](../mock-backend-py/server.py#L248-L257)). Its CSRF token is the
predictable string `csrf-<username>` and is shared by every session for that
user
([`server.py`](../mock-backend-py/server.py#L69-L93)). The token is also
returned by `/auth/whoami`
([`server.py`](../mock-backend-py/server.py#L396-L403)).

A live strict-mode request from `https://attacker.internal.example` received
the reflected origin, `Allow-Credentials: true`, and the authenticated
`root` response. A malicious same-site internal origin can therefore read GET
results using a victim's backend cookie and can obtain or guess the token for
POST requests. The precondition is realistic for sibling internal domains
because `SameSite` is not an origin boundary. Future mutating commands would
also inherit this exposure.

**Required change:** disable backend CORS in the normal server-to-server UI
topology. If direct browser access is necessary, use an exact origin allowlist,
reject `null`, emit `Vary: Origin`, and validate `Origin`/`Referer` on
state-changing requests. CSRF secrets must be random and session-bound, and
the cookie needs an explicit `SameSite` policy.

### SEC-06 — Attacker-controlled error text causes HTTP response-header injection

**Severity: High**

Missing paths are copied into error messages without validation
([`server.py`](../mock-backend-py/server.py#L63-L64) and
[`server.py`](../mock-backend-py/server.py#L167-L171)). `send_yt_error` writes
that message directly into `X-YT-Response-Message`
([`server.py`](../mock-backend-py/server.py#L278-L281)). Python's
`BaseHTTPRequestHandler.send_header` does not remove embedded CR/LF.

A raw-wire test using the JSON path
`"//missing\r\nSet-Cookie: injected=yes"` produced a real second
`Set-Cookie` response header. This is not contained at the backend boundary:
the UI error path streams the upstream response and forwards every header
except `content-length`, `vary`, and `www-authenticate`
([`index.ts`](../ytsaurus-ui/packages/ui/src/server/utils/index.ts#L81-L83) and
[`index.ts`](../ytsaurus-ui/packages/ui/src/server/utils/index.ts#L132-L147)).
An ordinary authenticated user—or any caller in anonymous mode—can therefore
plant arbitrary headers on a UI-origin response, manipulate cookies, and
create response parsing ambiguity in intermediaries.

**Required change:** never place raw error text in a response header. Omit the
header or strictly reject all control characters before header construction,
and keep detailed error data in the JSON body. The UI proxy should
defense-in-depth allowlist response headers and reject `Set-Cookie` on command
responses. Add a raw-socket regression covering parameters from query, header,
and body sources.

### SEC-07 — Passwords, robot tokens, and sessions travel over plaintext HTTP

**Severity: High under a hostile internal network**

The generated UI cluster hard-codes `secure: false`
([`_helpers.tpl`](../deploy/helm/iceberg-ui-mock/templates/_helpers.tpl#L136-L148)),
the UI always enables insecure YT authentication
([`ui.yaml`](../deploy/helm/iceberg-ui-mock/templates/ui.yaml#L76-L84)), and the
backend only serves HTTP. Ingress TLS is optional and empty by default
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L84-L93)).
The 30-day session cookie lacks both `Secure` and `SameSite`
([`server.py`](../mock-backend-py/server.py#L391-L394)).

Basic passwords, the robot bearer token, and user bearer cookies can therefore
be observed and replayed by an on-path internal component. TLS at the public
ingress alone does not protect the UI-to-backend hop.

**Required change:** require TLS for every authenticated ingress, configure the
UI cluster as secure, remove the insecure-auth override, and emit
`Secure; HttpOnly; SameSite=...` cookies. Protect UI-to-backend and
backend-to-PostgreSQL traffic with TLS/mTLS or an equivalent service-mesh
policy when the cluster network is in threat scope.

### SEC-08 — Unauthenticated clients can exhaust threads, memory, CPU, and request time

**Severity: High**

The Python server reads the complete request body before routing or
authenticating
([`server.py`](../mock-backend-py/server.py#L333-L346)). It accepts arbitrary
`Content-Length` values and accumulates arbitrary chunked bodies with no total
limit
([`server.py`](../mock-backend-py/server.py#L305-L315)). The five-second socket
timeout is installed only after a response is being sent
([`server.py`](../mock-backend-py/server.py#L259-L270)), while
`ThreadingHTTPServer` creates an unbounded thread per connection
([`server.py`](../mock-backend-py/server.py#L438-L450)). Thus many slow
`POST /ping` requests can hold threads indefinitely without ever reaching
authentication.

There are additional work amplifiers:

- `/login` has no rate limit; a wrong password for a known user performs
  PBKDF2-600k
  ([`userdb.py`](../mock-backend-py/userdb.py#L39-L57)), so parallel attempts
  can saturate CPU and reveal valid usernames by timing.
- `execute_batch` has no item, nesting, cost, or deadline limit and permits
  nested batches
  ([`server.py`](../mock-backend-py/server.py#L191-L225)). Configured
  `MOCK_DELAY` makes one large batch hold a worker long after the UI's request
  has timed out.
- backend, UI, and PostgreSQL resource limits default to empty objects
  ([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L18-L22),
  [`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L44-L46), and
  [`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L73-L79)).

**Required change:** use a production HTTP server or put equivalent limits in
the application: decoded-body and declared-length caps, immediate
header/body/idle deadlines, a bounded worker/concurrency queue, disconnect
cancellation, batch item/depth/work budgets, and a total command deadline.
Rate-limit login per source and account, bound password-hash concurrency, and
perform a dummy hash for unknown users. Enforce matching proxy limits and
Kubernetes requests/limits.

### SEC-09 — There is no network isolation between users/workloads, backend, and PostgreSQL

**Severity: High**

The chart contains no `NetworkPolicy`. The backend
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L129-L144)),
PostgreSQL
([`postgres.yaml`](../deploy/helm/iceberg-ui-mock/templates/postgres.yaml#L112-L126)),
and UI
([`ui.yaml`](../deploy/helm/iceberg-ui-mock/templates/ui.yaml#L133-L148)) are
ClusterIP Services. `ClusterIP` prevents public exposure by itself, but it does
not restrict other pods or corporate networks with routed Service access.

Any reachable workload can skip UI routing and call `/login` or API commands
directly. With PostgreSQL enabled, it can also target the identity store
directly. This makes the UI an interface, not a network security boundary.

**Required change:** add a default-deny policy and explicit flows for
ingress-controller-to-UI, UI-to-backend, backend-to-PostgreSQL, and required
DNS/operational traffic. Do not expose backend or PostgreSQL through Ingress,
NodePort, or LoadBalancer. Prefer distinct workload identities and, where
practical, a dedicated namespace.

### SEC-10 — Sessions cannot be reliably expired or revoked

**Severity: Medium**

PostgreSQL sessions have a 30-day server-side expiry but are stored as plaintext
bearer values
([`userdb.py`](../mock-backend-py/userdb.py#L21-L31) and
[`userdb.py`](../mock-backend-py/userdb.py#L156-L165)). In-RAM sessions store
only `cookie -> login` and never expire server-side
([`userdb.py`](../mock-backend-py/userdb.py#L204-L212)). Password changes do
not revoke existing sessions
([`userdb.py`](../mock-backend-py/userdb.py#L167-L174) and
[`userdb.py`](../mock-backend-py/userdb.py#L214-L216)).

There is no backend logout/revoke endpoint. UI logout only deletes browser
cookies
([`yt-auth.ts`](../ytsaurus-ui/packages/ui/src/server/components/yt-auth.ts#L11-L21)),
so a copied cookie remains usable for 30 days with PostgreSQL and indefinitely
in RAM until process restart.

**Required change:** enforce expiry in both stores, shorten and optionally idle
expire sessions, revoke them on logout and password reset, provide
administrator revoke-all, and delete expired rows. Store a hash of the session
token rather than the replayable token itself.

### SEC-11 — The hand-written HTTP framing accepts ambiguous or malformed requests

**Severity: Medium; higher behind a pooling HTTP intermediary**

The Python chunk decoder treats `Transfer-Encoding` as chunked only when its
entire lower-cased value is exactly `chunked`, accepts requests containing both
`Transfer-Encoding` and `Content-Length`, trusts arbitrary chunk sizes, does
not validate the two bytes after each chunk, and allows unlimited trailers
([`server.py`](../mock-backend-py/server.py#L305-L315)).

An upstream proxy can interpret duplicate/obfuscated framing differently and
leave bytes to be parsed as another request on a pooled backend connection.
The current UI/Axios path reconstructs most requests, which reduces the
cross-user smuggling risk, but a future ingress or service mesh may not have
identical parsing rules.

**Required change:** reject `Content-Length` plus `Transfer-Encoding`,
duplicates, unsupported encodings, invalid CRLF/trailers, and every overflow;
close the connection on a framing error. Prefer a maintained production HTTP
parser instead of custom chunk decoding.

### SEC-12 — Verb and command dispatch do not enforce a safe operation policy

**Severity: Medium; current commands are read-only**

Python maps GET, POST, PUT, and DELETE to the same handler
([`server.py`](../mock-backend-py/server.py#L405-L435)), while CSRF exempts GET
([`server.py`](../mock-backend-py/server.py#L90-L93)). A future mutating command
would therefore become GET-callable and bypass CSRF unless every author
remembers to add a separate guard.

The Node implementation also looks commands up on a normal object without an
own-property check. `/api/v3/constructor` and a batch entry named
`constructor` resolve inherited JavaScript properties instead of being
rejected
([`server.js`](../mock-backend/server.js#L300-L317) and
[`server.js`](../mock-backend/server.js#L468-L483)). A live check returned
`200`; current impact is limited to unintended behavior because the service is
read-only.

**Required change:** define allowed verbs and read/write classification per
command, return `405` for other methods, and enforce CSRF from operation
metadata rather than the caller's verb. In Node use a null-prototype map or
`Object.hasOwn`, and explicitly forbid nested/unknown batch commands.

### SEC-13 — Recording and administration workflows expose reusable credentials

**Severity: Medium when enabled**

Recording mode deliberately captures `Authorization`, `Cookie`, and
`X-Csrf-Token`, along with request and response bodies
([`server.py`](../mock-backend-py/server.py#L235-L239) and
[`server.py`](../mock-backend-py/server.py#L283-L303)). Basic credentials are
reversible; robot tokens and sessions are immediately replayable. The file is
unbounded, so a request flood can also consume disk. Normal request logging
prints the first 300 body bytes before authentication
([`server.py`](../mock-backend-py/server.py#L333-L341)).

The user administration CLI requires a password in the process argument list
([`userdb.py`](../mock-backend-py/userdb.py#L223-L231)), and deployment
documentation recommends passing secrets through Helm `--set`
([`README.md`](../deploy/README.md#L68-L77)). The robot token has no
`existingSecret` option. Secret-derived SHA-256 values are also placed in
readable pod annotations
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L30-L38)),
providing offline verifiers for weak values.

**Required change:** omit or irreversibly redact all credential headers,
restrict and rotate recordings, cap their size, and refuse recording in a
secure deployment profile. Read passwords from stdin/a Secret rather than
argv, support external Secret references for every credential, and use a
non-secret rotation revision instead of hashing secret material into
annotations.

### SEC-14 — The default backend workload is root and receives an unnecessary API token

**Severity: Medium**

The default path mounts source into stock `python:3.12-slim`
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L1-L14)), whose
default user is root. The chart exposes only an empty pod-level
`securityContext`
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L95-L99) and
[`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L108-L117)).
It does not enforce `runAsNonRoot`, `allowPrivilegeEscalation: false`, a
capability drop, read-only root filesystem, or RuntimeDefault seccomp.

The pod spec also omits `automountServiceAccountToken: false` and a dedicated
zero-RBAC ServiceAccount. A code-execution foothold can therefore steal the
namespace's default ServiceAccount token; impact depends on external RBAC
bindings. The optional baked backend image does use UID 65534
([`mock-backend.Dockerfile`](../deploy/docker/mock-backend.Dockerfile#L5-L11)),
but it is not the default deployment path.

**Required change:** make a baked, scanned, non-root image the supported path;
apply pod and container security contexts, drop all capabilities, disallow
privilege escalation, use a read-only filesystem with explicit writable
mounts, and set RuntimeDefault seccomp. Disable ServiceAccount token automount
for UI, backend, PostgreSQL, and test pods unless a narrowly scoped identity is
actually required.

### SEC-15 — Startup executes mutable, unpinned third-party code

**Severity: Medium**

With PostgreSQL and the default ConfigMap source mode, every pod start runs
`pip install 'psycopg[binary]'` from the network before starting the backend
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L48-L55)).
The version and hashes are not pinned, and this runs as root. The baked image
also installs an unpinned package and uses a mutable base tag
([`mock-backend.Dockerfile`](../deploy/docker/mock-backend.Dockerfile#L5-L8)).
Backend, PostgreSQL, and UI images are tag-based rather than digest-pinned
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L8-L11),
[`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L29-L32), and
[`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L54-L59)).

A compromised package release, package index, registry, or mutable tag becomes
code execution in a pod holding database and robot credentials.

**Required change:** build once in CI from a lockfile with hashes, pin base and
runtime images by digest, scan/sign/verify the result, and remove runtime
package installation and PyPI egress.

### SEC-16 — Direct-run defaults bind to every IPv4 and IPv6 interface

**Severity: Medium for local/direct execution**

`MOCK_HOST` defaults to `localhost:<port>` and is displayed as though it were
the listening address
([`server.py`](../mock-backend-py/server.py#L26-L30)), but the actual server
binds to the IPv6 wildcard and explicitly enables dual-stack IPv4
([`server.py`](../mock-backend-py/server.py#L438-L450)). Running the documented
`python3 server.py 8000` therefore exposes the service to every reachable
interface, not just loopback. The Node implementation also listens without an
explicit host.

**Required change:** separate advertised host from bind address, default
direct runs to loopback, and require an explicit wildcard bind for containers.

## Node implementation deployment warnings

The Helm chart deploys Python, so these are not included in the primary
deployment verdict. They must be fixed before deploying `mock-backend/server.js`
instead:

- **High — one malformed unauthenticated request target terminates the
  process.** `new URL(req.url, ...)` executes before the handler's `try`
  ([`server.js`](../mock-backend/server.js#L336-L382)). A raw
  `GET http://[invalid HTTP/1.1` request was reproduced on Node 20 and exited
  the process with an uncaught `ERR_INVALID_URL`. URL parsing and body handling
  need an outer error boundary plus `clientError` and rejected-promise
  handling.
- **Medium — session identifiers are not cryptographically random and never
  expire.** They use `Math.random()`
  ([`server.js`](../mock-backend/server.js#L131-L143)). Use
  `crypto.randomBytes` and the same expiry/revocation design as the Python
  implementation.
- Node also buffers request bodies without a limit
  ([`server.js`](../mock-backend/server.js#L93-L99)); SEC-08 applies equally.

## Positive controls observed

- SQL statements use parameters; no SQL injection path was found in the
  reviewed user store.
- New Python password hashes use PBKDF2-HMAC-SHA256 and comparisons use
  constant-time helpers.
- Python session tokens use `secrets.token_hex(16)`.
- Session cookies are `HttpOnly`.
- UI, backend, and PostgreSQL are ClusterIP Services, and Ingress is disabled
  by default.
- The optional baked backend image runs as a non-root UID.

These controls are useful but do not compensate for the deployment blockers
above.

## Recommended remediation order

1. Keep the service unexposed; make authentication fail closed (SEC-01).
2. Remove published application/robot credentials and revoke old sessions
   (SEC-02).
3. Replace and reduce PostgreSQL privileges, then isolate all three Services
   (SEC-03 and SEC-09).
4. Require TLS and secure cookie settings (SEC-07).
5. Close CORS/CSRF and response-header injection (SEC-05 and SEC-06).
6. Add request, concurrency, login, and batch budgets (SEC-08, SEC-11, SEC-12).
7. Implement the catalog authorization model before connecting real data
   (SEC-04).
8. Complete session, secret, workload, and supply-chain hardening
   (SEC-10 and SEC-13 through SEC-16).

Each numbered finding is intended to be independently testable and suitable
for a focused remediation commit. Some deployment fixes require coordinated
credential rotation; those commits should include explicit migration notes.

## Review limitations

This was a source and local wire-behavior review, not a live-cluster
penetration test. It did not inspect organization-specific ingress,
service-mesh, CNI, RBAC, storage encryption, secret-manager, or backup
configuration. It also did not perform a third-party dependency CVE/SBOM scan
or a complete security audit of the upstream ytsaurus-ui application.
