# Backend security review

Review date: 2026-07-27

Reviewed backend revision: `0d428fb4982db5f9d0775b81c7eaac1766e92158`

Reviewed UI boundary: `ghcr.io/ytsaurus/ui:1.60.1` and the matching
`ui-v1.60.1` source tag

## Executive result

The Python backend's strict-authentication path fails closed: missing,
expired, or incorrect user cookies and incorrect robot tokens are rejected. The
authenticated PostgreSQL Helm profile also renders with strict authentication,
does not create default users, and rejects the published robot and database
placeholders.

There is nevertheless one current authentication-boundary bypass in the
deployed UI layer. The stock UI authentication middleware continues to a
protected handler after any verifier failure other than HTTP 401. A malformed
cookie can trigger that path without a valid session. In the current chart an
unauthenticated caller can use it to obtain the fixed, robot-fetched
`cluster-params` response. That response currently contains only bootstrap
metadata, not arbitrary catalog rows, but it violates the intended fail-closed
boundary and must be fixed before the authentication guarantee can be relied
on.

Authentication is also the backend's only data-access decision. Every valid
local user and the shared robot can read every implemented catalog path. This
includes the live audit projection at `//sys/logs/audit_log`; the other current
catalog contents are synthetic. If all authenticated users are deliberately
one global-read role, that can be accepted as a documented product decision.
It is a deployment blocker if users are expected to have different
entitlements.

For an internal deployment, the other important access paths are credential
theft on plaintext hops, broad ClusterIP reachability, and compromise of the
PostgreSQL bootstrap-superuser credential used by the backend. Internal
placement alone does not close any of those paths.

## Scope and assumptions

The review covered:

- the Python HTTP, authentication, session, CSRF, command-dispatch, and audit
  code in [`mock-backend-py`](../mock-backend-py);
- the UI-to-backend password-authentication boundary implemented by the stock
  UI version selected in
  [`Chart.yaml`](../deploy/helm/iceberg-ui-mock/Chart.yaml);
- the Helm UI, backend, PostgreSQL, Secret, Service, and Ingress templates in
  [`deploy/helm/iceberg-ui-mock`](../deploy/helm/iceberg-ui-mock); and
- the default backend container and dependency-installation paths.

The review assumes:

- local passwords are verified by this service and users, sessions, the CSRF
  secret, and audit rows are persisted in the chart-managed PostgreSQL;
- delegated/external authentication is not configured;
- browser OAuth is not implemented and is outside this review;
- ordinary internal users can reach the UI;
- a malicious or compromised internal workload may be able to reach ClusterIP
  Services, but the attacker does not begin with Kubernetes administrator or
  host access;
- users must not obtain another user's session or data outside their intended
  entitlement; and
- denial of service is lower priority. Availability weaknesses are mentioned
  only where they help cross an authentication boundary or guess credentials.

Compose remains an anonymous, loopback-published development profile and is not
the reviewed internal-deployment profile.

## Current finding summary

| ID | Severity | Status | Finding |
|---|---|---|---|
| SEC-17 | **Medium** | Open | The stock UI authorization middleware fails open on non-401 verifier errors; an unauthenticated caller can currently receive robot-fetched bootstrap data |
| SEC-04 | **High** | Open / design decision | Every authenticated identity has global read access, including access to live audit metadata |
| SEC-07 | **High** | Open | User passwords, sessions, the robot token, and sensitive database traffic can cross plaintext hops |
| SEC-09 | **High** | Open | UI, backend, and PostgreSQL have no network isolation from other cluster workloads |
| SEC-03 | **High** | Open | The backend uses PostgreSQL's bootstrap superuser as its runtime identity |
| SEC-18 | **Medium** | Open | Weak local passwords and robot tokens are accepted, while login timing reveals known users and attempts are not throttled |
| SEC-13 | **Medium** | Open | The shared robot bearer token is unscoped, hard to attribute, and has a weak Secret lifecycle |
| SEC-10 | **Medium** | Open | A stolen session remains valid after UI logout and is stored as a replayable bearer value |
| SEC-19 | **Medium** | Open | Audit metadata is globally readable and rejected login attempts can spoof the `login` attribution field |
| SEC-14 | **Medium** | Open | Default workloads are insufficiently hardened and receive unnecessary Kubernetes API credentials |
| SEC-15 | **Medium** | Open | Default startup executes network-fetched packages as root and images are selected by mutable tags |
| SEC-12 | **Low, latent** | Open | HTTP verb and CSRF policy are not tied to command semantics |
| SEC-20 | **Low** | Open / accept | Health, version, API-version, and host-discovery routes are unauthenticated |

The severity of SEC-04 becomes low if global read access—including the audit
metadata described under SEC-19—is explicitly intended for every account. It
becomes the main confidentiality blocker as soon as catalog paths have
different entitlements.

## Findings

### SEC-17 — The deployed UI authorization middleware fails open

**Severity: Medium; fix before relying on the login boundary**

The UI version selected by the chart is `1.60.1`. Its
[`createAuthMiddleware`](https://github.com/ytsaurus/ytsaurus-ui/blob/ui-v1.60.1/packages/ui/src/server/middlewares/authorization.ts#L18-L41)
returns 401 only when the authentication check itself reports HTTP 401. A
timeout, network failure, malformed outgoing header, or backend 5xx falls
through to `next()`, including for non-UI routes that are supposed to be
protected.

This can be reached without a valid session. The password-auth resolver copies
the browser's cluster cookie into a backend `Cookie` header
([`yt-auth.ts`](https://github.com/ytsaurus/ytsaurus-ui/blob/ui-v1.60.1/packages/ui/src/server/middlewares/yt-auth.ts#L6-L23)).
For example, a URL-decoded NUL in that cookie makes Axios reject the outgoing
`/auth/whoami` request with `ERR_INVALID_CHAR`. That error is not classified as
an authentication failure, so the requested handler runs.

A targeted reproduction against the checked-out UI build demonstrated:

```text
GET /api/cluster-params/mock
Cookie: mock_YTCypressCookie=%00

HTTP 200
```

The same request without the malformed cookie returned 401. The exact
`ui-v1.60.1` source contains the same vulnerable middleware, resolver, cookie
decoding, and Axios/Node header path; the exact `1.60.1` container image was not
live-tested in this review. The handler uses the server-side robot through
[`getPreloadedClusterParams`](https://github.com/ytsaurus/ytsaurus-ui/blob/ui-v1.60.1/packages/ui/src/server/components/cluster-params.ts#L26-L36),
so the reproduction returned media and scheduler/master version bootstrap data
plus the expected missing/empty UI-configuration results. The current backend
does not let the caller choose an arbitrary path through this route, and the
reviewed response contained no table or audit rows. The impact is therefore
limited today, but this is a real authentication bypass, not only a future
guardrail issue.

Routes without `:ytAuthCluster` also take the non-401 fall-through path because
the middleware cannot construct a cluster setup. Their currently enabled
responses are low-sensitivity metadata or local utilities, and VCS is not
configured by this chart. Separately, configured robot-backed remote settings
and table-column presets would be affected by the general non-401 fall-through
path. Enabling a new UI-server integration without fixing the middleware could
silently expose it.

**Required change:** protected UI routes must fail closed for every failed or
incomplete authentication check. Return 401 for invalid credentials and
502/503 for verifier/storage/network errors. Only routes deliberately marked
public should bypass the check. Add regressions for malformed cookies, backend
500, timeout, missing `ytAuthCluster`, `cluster-params`, and every server-side
robot route. Deploy the fix in a rebuilt, digest-pinned UI image selected by
the chart, or enforce an equivalent independent gateway check; changing only a
standalone source checkout does not alter the stock image used by this release.

### SEC-04 — Authentication does not enforce object authorization

**Severity: High when users have different entitlements**

The backend authenticates once in
[`api_command`](../mock-backend-py/server.py#L856-L923), but `cmd_get`,
`cmd_list`, `cmd_read_table`, and `exists` do not use the authenticated identity
to authorize the requested path
([`server.py`](../mock-backend-py/server.py#L306-L414)).
`check_permission` and `check_permission_by_acl` unconditionally report
`allow`
([`server.py`](../mock-backend-py/server.py#L416-L420)).

As a result, every local account and the robot can enumerate and read every
implemented path. Most current rows are static mock fixtures. The exception is
`//sys/logs/audit_log`, whose rows are loaded from PostgreSQL through
[`userdb.audit_rows`](../mock-backend-py/userdb.py#L534-L537) and exposed by
[`data.py`](../mock-backend-py/data.py#L195-L214). This authenticated
global-read projection omits the free-form `details` value, but contains
timestamp, claimed/authenticated login, endpoint, and HTTP status.

UI permission checks are advisory and cannot substitute for enforcement in
the command handler.

**Required change:** either:

1. document and test that every account intentionally belongs to one
   global-read role, while applying a separate policy to audit metadata; or
2. define a deny-by-default path/table policy, enforce it before resolution and
   reads, make both permission-reporting commands use the same policy, and
   scope the robot to only its required bootstrap paths.

Do not attach data with different user entitlements until the second model is
implemented.

### SEC-07 — Authenticated traffic is not required to use TLS

**Severity: High when the internal network or an intermediary is not fully trusted**

The generated cluster configuration hard-codes `secure: false`
([`_helpers.tpl`](../deploy/helm/iceberg-ui-mock/templates/_helpers.tpl#L176-L188)),
the UI always sets `YT_AUTH_ALLOW_INSECURE=1`
([`ui.yaml`](../deploy/helm/iceberg-ui-mock/templates/ui.yaml#L76-L84)), and the
backend exposes HTTP only. Ingress TLS is optional and empty by default
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L107-L116)).
The PostgreSQL connection also has no TLS configuration
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L82-L98)).

The backend itself emits `Secure; HttpOnly; SameSite=Lax` session cookies
([`server.py`](../mock-backend-py/server.py#L837-L841)). However, the UI is
explicitly allowed to remove `Secure` when the browser origin is HTTP, and TLS
at an external ingress still leaves the UI-to-backend Basic password, session
cookie, and robot-token hops in plaintext. An on-path component can replay any
of those bearer credentials.

**Required change:** require HTTPS and redirect/HSTS at every authenticated
user ingress. Do not expose the UI Service directly to an HTTP user network.
Use TLS/mTLS or an equivalent service-mesh transport for UI-to-backend and
backend-to-PostgreSQL when the pod network is in scope. Restrict insecure-cookie
stripping to an explicit local-development profile.

### SEC-09 — The chart does not isolate service reachability

**Severity: High under the reviewed internal-workload threat model**

There is no `NetworkPolicy` in the chart. UI, backend, and PostgreSQL are all
ClusterIP Services
([`ui.yaml`](../deploy/helm/iceberg-ui-mock/templates/ui.yaml#L133-L148),
[`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L138-L153),
and
[`postgres.yaml`](../deploy/helm/iceberg-ui-mock/templates/postgres.yaml#L130-L144)).
In the absence of other cluster policy, pods are non-isolated by default; a
ClusterIP is a routing choice, not an authorization boundary
([Kubernetes NetworkPolicy behavior](https://kubernetes.io/docs/concepts/services-networking/network-policies/)).

Another workload can bypass the UI and address `/login`, backend API routes,
and PostgreSQL directly. Strong application/database credentials still gate
those services, so reachability alone is not an authentication bypass. It does,
however, expose every credential boundary and makes SEC-03, SEC-07, SEC-13,
and SEC-18 materially easier to exploit.

**Required change:** apply namespace default-deny ingress and egress, then
allow only ingress-controller/gateway to UI, UI to backend, backend to
PostgreSQL, and the required DNS/operational flows. If Helm tests remain
enabled, give only the labeled smoke pod the temporary flows it needs. Verify
that the cluster's CNI actually enforces NetworkPolicy. The current
ConfigMap-source backend also needs PyPI egress at every start; switch to the
baked image before denying that egress, or temporarily allow only a controlled
package proxy. Do not expose backend or PostgreSQL with Ingress, NodePort, or
LoadBalancer. Prefer a dedicated namespace and distinct workload identities.

### SEC-03 — The backend holds a PostgreSQL superuser credential

**Severity: High because compromise permits authentication-store takeover**

The chart passes one configured role as `POSTGRES_USER`
([`postgres.yaml`](../deploy/helm/iceberg-ui-mock/templates/postgres.yaml#L75-L84))
and gives the same role and password to the backend
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L82-L98)).
The official PostgreSQL image creates the role named by `POSTGRES_USER` with
superuser power
([official image documentation](https://github.com/docker-library/docs/blob/master/postgres/README.md#postgres_user)).

The published password is correctly rejected, but a backend compromise or
theft of the replacement password still permits an attacker to read plaintext
session tokens, replace password hashes, create sessions, change the CSRF
secret, or exercise database-superuser capabilities. That crosses the
application authentication boundary without knowing a user's password.

The current connection initializer executes the full `SCHEMA` DDL whenever it
opens or replaces a connection
([`userdb.py`](../mock-backend-py/userdb.py#L364-L383)). A DML-only runtime role
cannot be introduced until that initialization is separated.

**Required change:** move schema creation/reconciliation to a one-time
owner-controlled initialization Job, then use a least-privilege runtime role.
Grant that role only the specific tables, sequences, and operations it needs,
keep the owner credential out of the backend pod, and permit PostgreSQL ingress
only from the backend. Rotate the database credential and revoke all sessions
after any suspected exposure.

### SEC-18 — Credential strength and online guessing are not controlled

**Severity: Medium**

`set_password` accepts any string, including empty and one-character passwords
([`userdb.py`](../mock-backend-py/userdb.py#L504-L516)), and the administration
CLI does not enforce a policy
([`userdb.py`](../mock-backend-py/userdb.py#L667-L697)). The UI rejects empty
passwords, but direct HTTP Basic login does not. Robot validation rejects only
an empty value and the published placeholder
([`_helpers.tpl`](../deploy/helm/iceberg-ui-mock/templates/_helpers.tpl#L126-L131));
a one-character robot token is valid.

Login responses mask unknown users, but their timing does not. A known login
with a wrong password performs PBKDF2 while an unknown login returns after the
database lookup
([`userdb.py`](../mock-backend-py/userdb.py#L451-L476)). `/login` has no source,
account, or concurrency throttling
([`server.py`](../mock-backend-py/server.py#L788-L835)). This supports username
enumeration and targeted online password/token guessing. The concern here is
account takeover, not denial of service.

**Required change:** reject empty passwords at minimum and define an
operator-approved password policy. Generate a high-entropy robot token instead
of accepting arbitrary short input. Apply bounded login and robot-token
attempt throttling at the gateway/backend, and perform equivalent password-hash
work for unknown users so the response timing does not disclose account
existence.

### SEC-13 — The robot credential is shared, unscoped, and difficult to attribute

**Severity: Medium**

The fixed robot token is an exact bearer credential. It bypasses the
cookie-CSRF check and maps to the hard-coded login `iceberg`
([`authenticate`](../mock-backend-py/server.py#L264-L288)). It can call every
command and path, even though the UI needs it only for specific server-side
bootstrap reads. A local human account may also be named `iceberg`, making
audit attribution ambiguous and making future name-based authorization
dangerous.

The chart accepts the token only as `auth.robotToken`, renders it into the UI
Secret, mounts that Secret into the UI, and injects the same Secret value into
the backend environment
([`ui.yaml`](../deploy/helm/iceberg-ui-mock/templates/ui.yaml#L14-L28) and
[`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L73-L80)).
There is no external-Secret reference for it. A token-derived SHA-256 value is
placed in readable backend and UI pod annotations
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L31-L38)
and [`ui.yaml`](../deploy/helm/iceberg-ui-mock/templates/ui.yaml#L47-L50)),
which provides offline verifiers for a weak token. The Helm smoke pod also
receives the production robot Secret and uses a mutable-tag curl image, with no
hook deletion policy
([`test-smoke.yaml`](../deploy/helm/iceberg-ui-mock/templates/tests/test-smoke.yaml#L1-L25)).
Compromise of the UI pod, backend pod, test image while it runs, Helm release
values, or Secret grants direct backend access until manual rotation.

**Required change:** create a reserved robot principal distinct from every
human login, record the authentication mechanism in audit rows, and authorize
only the fixed bootstrap commands/paths it needs. Support an existing/external
Secret, require high entropy, use a non-secret rotation revision in pod
annotations, restrict Secret RBAC, and document rotation. Do not pass the
production robot to a general smoke image; use a scoped short-lived test
credential if authenticated smoke checks are required, and clean up completed
test hooks.

### SEC-10 — Logout does not revoke a server session

**Severity: Medium**

Sessions are 256-bit random and expire server-side, but the default lifetime is
30 days. PostgreSQL stores the bearer cookie itself as the session primary key
([`userdb.py`](../mock-backend-py/userdb.py#L100-L105)), and session lookup
accepts it until expiry or a password-revision change
([`userdb.py`](../mock-backend-py/userdb.py#L478-L494)).

The backend has no logout or per-session revocation route. UI logout deletes
browser cookies only
([`yt-auth.ts`](https://github.com/ytsaurus/ytsaurus-ui/blob/ui-v1.60.1/packages/ui/src/server/components/yt-auth.ts#L11-L21)).
A copied cookie therefore remains usable for direct backend calls after the
user sees a successful logout. Changing the user's password correctly
deletes/revokes all sessions and is the current emergency control.

**Required change:** add an authenticated backend logout that revokes the
current session and administrator revoke-one/revoke-all operations. Shorten the
default, expose and validate `MOCK_COOKIE_TTL_SECONDS` in Helm, and consider
idle expiry. Store a hash of the session token rather than the replayable
value, and rotate/revoke sessions after credential or database exposure.

### SEC-19 — Audit visibility and attribution do not form a security boundary

**Severity: Medium**

Every authenticated identity can read the audit projection described in
SEC-04. It reveals who used the service, when, which endpoint was called, and
the response status. That can expose account names and operational activity
even if global catalog read is intended.

For `/login`, the backend assigns `request.state.audit_user` from the claimed
Basic username before verifying the password
([`server.py`](../mock-backend-py/server.py#L810-L835)). An unauthenticated
caller can therefore insert a rejected audit row whose `login` column names any
chosen user. The accompanying HTTP 401 still distinguishes the attempt from a
successful login, but the column cannot be treated as an authenticated actor.
Robot calls and a human named `iceberg` are also indistinguishable.

The user-controlled audit payload is credential-redacted, bounded below 1,000
bytes, and its free-form details are not exposed through the catalog table.
Those controls reduce payload leakage but do not solve access or attribution.

**Required change:** authorize audit reads separately, preferably to an
administrative/auditor role. Store authenticated actor and claimed login in
separate fields, leave actor null for a rejected login, record the
authentication mechanism/principal, and document the meaning of each column.

### SEC-14 — Workloads lack post-compromise containment

**Severity: Medium**

The default ConfigMap-source backend runs in stock `python:3.12-slim` as root
and installs packages before startup
([`values.yaml`](../deploy/helm/iceberg-ui-mock/values.yaml#L7-L18) and
[`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L45-L55)).
The chart provides only an empty, shared pod-level `securityContext` by
default. It does not enforce per-container `runAsNonRoot`,
`allowPrivilegeEscalation: false`, capability dropping, a read-only root
filesystem, or RuntimeDefault seccomp; PostgreSQL and the smoke pod have no
equivalent controls.

No pod sets `automountServiceAccountToken: false` or selects a dedicated
zero-RBAC ServiceAccount. Kubernetes otherwise supplies the assigned/default
ServiceAccount credential to the pod
([Kubernetes ServiceAccount documentation](https://kubernetes.io/docs/concepts/security/service-accounts/)).
A code-execution foothold therefore gains the pod's robot/database material
and a Kubernetes API identity whose impact depends on cluster RBAC.

The optional baked backend image does run as UID/GID 65534
([`mock-backend.Dockerfile`](../deploy/docker/mock-backend.Dockerfile#L1-L12)),
but it is not the default.

**Required change:** make a baked non-root image the supported deployment,
define per-workload pod and container security contexts, drop all capabilities,
disallow privilege escalation, use RuntimeDefault seccomp and a read-only root
filesystem with explicit writable mounts, and disable API-token automount for
UI, backend, PostgreSQL, and test pods. Use dedicated zero-RBAC service
accounts where identities are operationally required.

### SEC-15 — Startup executes mutable third-party artifacts

**Severity: Medium**

The default backend pod runs `pip install` from the network as root on every
start
([`mock-backend.yaml`](../deploy/helm/iceberg-ui-mock/templates/mock-backend.yaml#L49-L55)).
Every resolved Python distribution is exact-version-pinned
([`requirements.txt`](../mock-backend-py/requirements.txt)), which prevents
ordinary version drift, but hashes are not pinned. Backend, PostgreSQL, and UI
images are also tag-selected rather than digest-selected.

A compromised package index/release, registry, or mutable tag therefore
becomes code execution in a pod holding the robot or database credential. This
is an unexpected-access path even though it is not directly reachable through
an HTTP request.

**Required change:** build once in CI from a hash-locked dependency set, pin
base and runtime images by digest, scan/sign/verify the result, and remove
runtime package installation and PyPI egress.

### SEC-12 — Command policy is based on the request verb, not the operation

**Severity: Low and latent while every implemented command is read-only**

The API route accepts GET, POST, PUT, and DELETE for every command, while CSRF
is waived for GET and for robot-token requests
([`server.py`](../mock-backend-py/server.py#L281-L288) and
[`server.py`](../mock-backend-py/server.py#L856-L867)). `execute_batch` invokes
the same command map recursively
([`server.py`](../mock-backend-py/server.py#L392-L405)).

All current implemented data commands are read-only, so this does not create a
current mutation or privilege-escalation exploit. A future write/admin command
would be GET-callable and cookie-CSRF-exempt unless the author adds an
independent guard.

**Required change before adding writes:** attach allowed verbs, read/write
classification, required principal/permission, and CSRF requirements to
command metadata. Enforce the same policy for every batch subcommand and
return 405 for unsupported verbs.

### SEC-20 — Limited infrastructure metadata is public

**Severity: Low**

`/ping`, `/ready`, `/version`, `/service/version`, `/hosts*`, and `/api[/]`
do not authenticate
([`server.py`](../mock-backend-py/server.py#L731-L785)). They expose liveness,
storage readiness, a mock version, the advertised backend address, and
supported API versions. They do not expose catalog rows, users, sessions, or
audit details.

The global exception wrapper also returns `str(exception)` to the client
([`server.py`](../mock-backend-py/server.py#L691-L718)). An unauthenticated
login or identity check that encounters a database failure may therefore reveal
internal host, database, or schema details. No password or bearer-token
disclosure was observed.

**Required change or acceptance:** keep `/ping` and `/ready` available only to
the intended Kubernetes probes and UI/gateway through network policy. Remove
or restrict host/version discovery if that metadata is not operationally
required; otherwise document it as intentionally public inside the service
boundary. Return a generic 500 body/header to clients and log detailed
exceptions server-side.

## Revalidated controls

The following previously problematic entry paths are closed in the reviewed
profile:

| Previous ID | Result |
|---|---|
| SEC-01 | Helm rendering fails unless PostgreSQL/strict authentication is configured or an explicit development-only anonymous opt-in is supplied. |
| SEC-02 | Strict mode has no seed users and rejects the published user-password pairs and robot placeholder. |
| SEC-05 | CORS is default-deny with exact origins; CSRF uses a persisted random HMAC secret and constant-time validation; the backend cookie is `Secure`, `HttpOnly`, and `SameSite=Lax`. |
| SEC-06 | Error-header control characters are escaped. |
| SEC-11 | FastAPI/uvicorn replaced the hand-written HTTP parser. |
| SEC-13, recording portion | Recording redacts credential-shaped values, bounds bodies/files, and cannot start with authentication enabled. |
| SEC-16 | Direct runs bind to loopback unless an explicit wildcard bind is configured; Compose publishes its anonymous UI on host loopback only. |

Additional positive controls observed:

- PBKDF2-HMAC-SHA256 uses per-user random salts, a 600,000-iteration floor,
  bounded verification work, and constant-time comparison.
- Session tokens contain 256 bits of randomness, expire server-side, and are
  password-revision-bound. Password changes revoke all existing sessions.
- SQL values are parameterized; catalog path resolution accesses only the
  in-memory node tree and does not perform filesystem traversal.
- Unknown commands and routes return errors; recording and user-administration
  functions are not exposed as HTTP command handlers.
- Audit payloads are credential-redacted, depth/width/size-bounded, and the
  browsable projection excludes the schemaless details.
- The authenticated Helm mode rejects the published robot/database
  placeholders and the render regression suite passes.

## Recommended remediation order

1. Make the UI authentication middleware fail closed and add the malformed
   cookie/non-401 regressions (SEC-17).
2. Decide whether all authenticated users intentionally have global read
   access. If not, implement server-side path authorization. In either case,
   restrict audit reads and fix attribution (SEC-04 and SEC-19).
3. Require HTTPS at the user boundary and protect internal credential hops
   (SEC-07).
4. Move the backend and UI to built, digest-pinned artifacts so runtime PyPI
   egress can be removed (SEC-15).
5. Add NetworkPolicy and replace the PostgreSQL bootstrap-superuser runtime
   role (SEC-09 and SEC-03).
6. Enforce credential strength/throttling and reduce the robot token's scope
   and exposure (SEC-18 and SEC-13).
7. Implement backend session revocation and hashed session storage (SEC-10).
8. Complete non-root workload and ServiceAccount hardening (SEC-14).
9. Add command/verb/CSRF metadata before implementing any mutating command
   (SEC-12).

Request-body, batch-work, login-work, and Kubernetes resource budgets remain
incomplete, but availability-only denial-of-service work is intentionally not
ranked in this review.

## Verification performed

- Source review of the current backend revision and Helm templates.
- Source review of the exact `ui-v1.60.1` UI tag selected by the chart.
- `python3 tests/test_userdb.py`: 22 tests passed.
- `bash deploy/helm/iceberg-ui-mock/tests/test-auth-render.sh`: passed.
- Targeted live reproduction of the malformed-cookie UI bypass against the
  checked-out UI build, plus source-path verification in exact tag
  `ui-v1.60.1`; the exact `1.60.1` container was not live-tested.
- `python3 db/sync.py check`: generated documentation indexes are current.

## Limitations

This was a source, rendering, and targeted local behavior review, not a
live-cluster penetration test. It did not inspect organization-specific
Ingress/Gateway configuration, service mesh, CNI enforcement, RBAC bindings,
Secret encryption, storage/backup controls, or image-admission policy. It did
not run a dependency CVE/SBOM scan or audit the complete upstream UI beyond the
password-authentication and robot-backed routes relevant to this deployment.
