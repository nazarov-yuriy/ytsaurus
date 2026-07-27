# Google OAuth for the Iceberg viewer — design notes

These are design notes. The UI state-validation hardening described below is
implemented only in the ignored, standalone `ytsaurus-ui` source checkout.
Parent-repository commits do not contain that source, and the project
Helm and Compose deployments currently reference stock
`ghcr.io/ytsaurus/ui:1.60.1`, so the hardening is **not deployed or deliverable
yet**. The reviewed UI change is preserved as standalone commit
`5b1856f41c779cb0fdd79b9aaa6655307a549fe2`; it needs to be upstreamed or built
into an explicitly pinned custom image first. Google identity validation and
delegation also remain
unimplemented. Two distinct problems hide in this request:

1. **Sign-in**: authenticate UI users with Google accounts without changing
   the browser-facing ytsaurus-ui login experience.
2. **Delegation**: access GCS-stored Iceberg files *as* those users, not as a
   shared service identity.

They are separable — a sign-in choice does not lock in a delegation choice —
and conflating them is the main design trap here.

## Ground truth (verified in source, this repo)

The UI already supplies most of the OAuth plumbing, but its callback must be
hardened before it is safe to use:

- The UI ships a generic OAuth authorization-code client:
  `GET /oauth/login` → 302 to `<authPath>?response_type=code&client_id&scope&
  redirect_uri=<origin>/api/oauth/callback&state=…`; the callback exchanges the
  code (with `client_secret`, form-encoded — Google-compatible) and stores
  `__Host-yt_oauth_access_token` / `__Host-yt_oauth_refresh_token` cookies in
  the hardened checkout (`ui/src/server/components/oauth.ts`, docs/auth.md
  §3.4). Enabled purely by
  Node config `ytOAuthSettings {baseURL, authPath, tokenPath, clientId,
  clientSecret, scope, callbackBaseUrl}`; `callbackBaseUrl` is a required,
  explicitly configured public origin and is never derived from the request
  `Host` header. The upstream Helm chart checkout exposes it as
  `settings.oauth.callbackBaseUrl` and rejects an enabled OAuth configuration
  that omits it. The login page shows the SSO button whenever `allowOAuth` is
  true (`LoginFormPage.tsx:151-164`).
- The original callback treated `state` only as the name of a return-path
  cookie and exchanged the code even when that cookie was absent. That is login
  CSRF/session swapping, not a complete OAuth client. Naming the cookie after
  `state` is also vulnerable to parent-domain cookie tossing. The hardened
  checkout instead uses one fixed `__Host-yt_oauth_state` cookie containing the
  generated state and local return path, rejects duplicate raw cookie entries,
  compares callback state in constant time, and consumes the cookie before code
  exchange. The `__Host-` contract requires `Secure; Path=/` and forbids
  `Domain`; the cookie is also `HttpOnly; SameSite=Lax` with a 10-minute
  lifetime. Saved return paths are local and byte-bounded.
- OAuth access and refresh cookies also use fixed `__Host-` names with
  `HttpOnly; Secure; SameSite=Lax; Path=/`, no `Domain`, and identical
  attributes when cleared. This intentionally invalidates existing OAuth
  browser sessions once and requires users to sign in again. Token-exchange and
  refresh failures return/log only generic errors, since raw Axios errors can
  contain the code, refresh token, or client secret; a failed refresh also
  clears stale token cookies rather than retrying the IdP on every request. A
  deployed UI image **must contain these fixes**; the current stock image is not
  safe for Option A.
- URL building is `new URL(config.authPath, config.baseURL)` — an **absolute
  `authPath`/`tokenPath` overrides `baseURL`**, so Google's two-host layout
  (`accounts.google.com` authorize, `oauth2.googleapis.com` token) fits without
  any proxying tricks.
- On every proxied API call the UI turns the cookie into
  **`Cookie: access_token=<token>`** toward the cluster proxy
  (`middlewares/oauth.ts:19-24`, docs/auth.md §2.2). The real YT proxy's
  composite cookie authenticator explicitly claims an `access_token` cookie
  (`auth_server/public.h:136-141`, docs/auth.md §4.4) and validates it against
  a configured userinfo endpoint. **Our backend does not read that cookie yet**
  (`server.py authenticate()`, lines 224–238, knows only `YTCypressCookie` and
  `Authorization: OAuth`) — that one branch is the entire backend gap.
- Two Google-specific rough edges in the UI client:
  - `url.search = params.toString()` **overwrites** any query baked into
    `authPath`, so we cannot smuggle `access_type=offline&prompt=consent` into
    the authorize request → **Google will never return a refresh token** in
    this flow. Sessions silently cap at the access token's ~1 h.
  - The token-response type expects Keycloak's `refresh_expires_in`; Google
    omits it. Harmless while there is no refresh token (the refresh-cookie
    branch is skipped), but a broker that *does* return `refresh_token` without
    `refresh_expires_in` would compute `maxAge: NaN` — verify before relying
    on refresh.
- `/auth/whoami` stays the single gate, and our CSRF is our own HMAC. An
  `access_token` cookie branch must be classified as cookie authentication so
  mutating commands still require CSRF; treating the bearer as header/robot
  authentication would silently bypass that check. The boot-path robot
  requests (`cluster-params`) keep using the robot token and are unaffected.

## Option A — UI-native OAuth straight against Google

`ytOAuthSettings` with absolute Google URLs, `scope: "openid email"`, and a
pathless HTTPS `callbackBaseUrl` such as `https://viewer.example.internal`
(plain HTTP is accepted only for an explicit loopback address in local
development).
Backend adds one authenticate() branch: `access_token` cookie → validate
against Google → resolve the stable `(issuer, sub)` principal, auto-provision
it in a provider-specific namespace (origin `google`), and attach identity.
Email is display metadata, not a database key.

Validation is mandatory, not a menu of equivalent choices:

1. Introspect the opaque access token at Google's pinned tokeninfo endpoint.
   Require the expected provider/issuer, `aud` and authorized party for our
   client, unexpired `exp`, and all required scopes. Userinfo alone is
   insufficient because it does not bind the token to our OAuth client; that
   would accept tokens issued to other applications.
2. Require a stable non-empty `sub`. If userinfo is used for profile
   enrichment, its `sub` must match the introspection result.
3. Require `email_verified == true` before retaining email. If access is
   organization-only, validate the `hd` claim against an explicit allowlist;
   an email suffix and the authorization request's `hd` hint are not security
   checks.
4. An ID token verified locally via JWKS would avoid the introspection RTT, but
   the UI stores only the access token, so that requires a different flow or a
   UI change.

Cache verdicts by token hash **and provider/client configuration**, with a
short TTL no later than token expiry. Never place the token in audit details,
recordings, URLs, or exception logs: it is a bearer credential. The backend
now structurally redacts credential-shaped audit fields and refuses traffic
recording whenever strict or delegated authentication is enabled. Those are
defense in depth; OAuth call sites must still avoid passing bearer values to
either facility in the first place.

Verdict: smallest possible delta, real Google login, but **1-hour sessions
with a manual re-click** (after expiry the UI 401s → login page → SSO button →
silent redirect through Google → new token). Fine for a viewer; annoying for
long sessions. No refresh token also means no long-lived GCS delegation from
this flow.

## Option B — an IdP broker (Dex / Keycloak) between UI and Google

UI does OAuth against the broker; the broker federates "Log in with Google".
Backend validates broker-issued JWTs offline via JWKS — no Google round-trip
per request. Compatibility is not generic: the current UI expects
Keycloak-style `refresh_expires_in`. A broker such as Dex that returns a
standard refresh-token response without that extension needs an adapter/UI fix
and a captured-response compatibility test before refresh can be called
working.

Extra wins: the broker is where refresh tokens, session lifetime, allowed
domains (`hd` claim), and future group/role claims live; Keycloak can **store
the upstream Google tokens** and re-issue them via token exchange — which is a
clean answer to the delegation problem too. Cost: one more stateful service to
deploy and operate (Dex is the lightweight end of that spectrum).

Verdict: the right production shape if this grows past a demo; overkill for a
first spike.

## Option C — the nginx idea (your proposal), assessed

> route a single path like "/login" to our page that sets correct cookies

This works, and it degenerates into something simpler than a separate login
page. The cookies that make an unmodified UI "logged in" are known
(docs/auth.md §1.4, §5.4): the UI tunnel forwards
`<cluster>_YTCypressCookie` (e.g. `mock_YTCypressCookie`) back to the proxy as
`YTCypressCookie`. Anything same-origin that sets that cookie logs the user
in; the login form is never involved.

Refined version — **the backend is the login page**:

```
browser ── nginx (or the existing ingress) ──┬── /*                → ytsaurus-ui (unmodified)
                                             ├── /google/login    → mock backend: mint state + nonce + PKCE verifier,
                                             │                      302 to Google (access_type=offline)
                                             └── /google/callback → require/consume state; exchange code with verifier;
                                                                    verify id_token + nonce; provision (iss, sub);
                                                                    bind Google grant to a new local session;
                                                                    Set-Cookie: mock_YTCypressCookie=<64-hex>; Path=/
                                                                    → 302 /mock
```

- No UI change, no YT change; nginx only adds two same-origin routes (in the
  Helm chart this is two extra `location`s on the ingress, or a tiny sidecar).
- The backend already has everything else: session mint (`external_login`
  pattern), whoami, CSRF, audit (login outcomes incl. `origin=google`).
- The authorization request needs a high-entropy, one-time `state` bound to a
  short-lived `HttpOnly; Secure; SameSite=Lax` browser cookie or server session,
  plus an OIDC `nonce` and PKCE verifier. The callback consumes state before
  exchanging the code and validates the ID-token signature, pinned issuer,
  `aud`/`azp`, expiry/issued-at, nonce, stable `sub`, verified email and allowed
  hosted domain. Redirect URIs come from fixed configuration, never Host or
  forwarding headers supplied by the caller.
- Because **we** build the authorize URL, `access_type=offline&prompt=consent`
  is available → refresh tokens → long sessions *and* durable GCS delegation.
  Each local session must reference the exact encrypted Google grant used to
  create it; a loose token stored per email/user would mix concurrent sessions
  and consent sets. The local session may outlive a one-hour access token only
  while refresh/revalidation succeeds. Account disablement, token revocation or
  refresh failure revokes the local session rather than leaving it valid for
  the default 30 days.
- Entry UX: unauthenticated users must land on `/google/login`. Either an
  nginx `auth_request` gate on `/` (redirect when the backend says
  no-session), or just tell users the URL / bookmark — the plain UI 401 page
  would otherwise show the (useless) password form, since we cannot add a
  button to it without modifying the UI. This is the one cosmetic wart.
- Cookie details to honor: `Path=/`, `HttpOnly`, `Secure` (Google mandates
  https redirect URIs except on localhost), explicit `SameSite=Lax`, and the
  cluster-prefixed name must match the cluster id in `clusters-config.json`.

Verdict: **best effort-to-payoff for us**, precisely because it moves the
OAuth client out of the UI (whose client is the limiting factor) into code we
own — while the UI still runs unmodified.

### C2 — the GCP-managed variant: Identity-Aware Proxy

Since deployment is heading to GCP anyway: put IAP in front of the whole UI.
Google handles login/session and the request reaching the **UI** carries a
signed `X-Goog-IAP-JWT-Assertion`. The unmodified UI does not forward that
header on its server-side `/auth/whoami` boot request, even though its generic
command tunnel forwards most incoming headers. A backend-only branch therefore
cannot authenticate the boot path.

C2 needs either (a) UI middleware that verifies the assertion and constructs
the normal `req.yt` authentication headers, or (b) a same-origin ingress
exchange that verifies IAP and mints the cluster-prefixed local session cookie.
In both shapes, validate the IAP signature, pinned issuer, exact backend-service
audience and expiry on every protected request, and prevent network paths that
bypass IAP. IAP proves *identity* only — it does not hand us a GCS-scoped user
token, so delegation still needs one of the mechanisms below.

## The delegation problem (GCS access on behalf of the user)

What we ultimately need: when `data.py` becomes an Iceberg catalog reader,
its GCS reads should be authorized as the signed-in user. Mechanisms, in
increasing order of trust shifted onto us:

1. **User access token, per session** — request
   `devstorage.read_only` scope at consent; use the token for GCS until it
   expires (~1 h). True GCP IAM enforcement per user. Option A keeps the token
   in the UI's protected cookie and forwards it per request; C1 must retain it
   in a bounded, encrypted server-side session grant. This is no long-term
   refresh-token storage, but it is still storage of a bearer secret.
2. **Refresh tokens stored server-side** (PG, encrypted, revocable) — durable
   on-behalf access; we become a credential store, with all that implies
   (KMS-backed envelope encryption and rotation, least-privilege DB access,
   deletion/revocation, backup policy, incident blast radius). Use a separate
   provider/grant table keyed by issuer, subject and OAuth client, not an opaque
   column on `users`; grants have their own scopes, expiry and rotation state.
3. **Broker token exchange** (option B): Keycloak keeps the Google tokens,
   backend exchanges its own JWT for a fresh Google access token on demand —
   same power as 2 with the storage moved into the broker.
4. **Service account + Credential Access Boundaries** — one SA, per-request
   *downscoped* short-lived tokens (STS) limited to prefixes the user may
   read. GCP enforces the boundary but **we** decide the mapping user→prefix:
   authorization moves into our code. Honest fit for the current mock, which
   already stubs `check_permission` as allow-all.
5. **Domain-wide delegation** — SA impersonates any Workspace user. Admin
   consent, org-only, enormous blast radius; note for completeness, avoid.

Practical notes regardless of mechanism:
- `devstorage.*` is a **sensitive scope** → Google app verification, unless
  the OAuth app is Workspace-internal or stays in testing (≤100 test users).
- Ask for the storage scope with **incremental consent** (second consent
  after login), so plain sign-in stays a non-scary `openid email` prompt. Bind
  that second flow to the already-authenticated `(issuer, sub)` session and
  reject a grant returned for a different subject.
- Tokens expire mid-query: long `read_table` paths need refresh-before-use,
  not refresh-on-401.
- If the catalog side lands on an Iceberg **REST catalog** (e.g. BigLake),
  verify the catalog's required issuer, audience and scopes before forwarding
  anything. A GCS-scoped bearer must never be sent automatically to an
  arbitrary catalog URL.

## Suggested spike order (cheapest disproof first)

1. **Fake-Google spike, no Google account needed**: extend the
   `test_external_auth.py` pattern — an in-process fake issuing codes/tokens
   with Google's response shapes; wire `ytOAuthSettings` at a dev UI against
   it; use a UI build containing the state fix; add the `access_token`-cookie
   branch to `authenticate()`. Cover missing/mismatched/replayed state, foreign
   audience/authorized party, wrong issuer, expired token, missing scope,
   unverified email and hosted-domain rejection. This proves the full Option A
   plumbing end-to-end in CI.
2. **Real Google on localhost** (http loopback redirect URIs are allowed):
   confirm consent screen, `aud` validation, the 1-hour expiry UX.
3. Decide A (fastest demo) vs C1 (owns the flow, enables delegation) vs B
   (production shape). A→C1 is not throwaway: the token-validation branch and
   provisioning are shared; C1 adds two routes and the authorize-URL builder.
4. Delegation after sign-in settles; start with mechanism 1 (session-scoped
   user token) and only escalate to stored refresh tokens once the Iceberg
   read path actually exists.

## Open questions / to verify empirically

- Does the UI recover gracefully when `__Host-yt_oauth_access_token` expires
  mid-session (expected: 401 → login page with SSO button; confirm no retry
  storm)? What does logout look like with Google (no RP-initiated logout
  endpoint; `logoutPath` unset → cookie-clearing callback only)?
- Express `res.cookie` behavior with `maxAge: NaN` (broker-without-
  `refresh_expires_in` case) — broken cookie or dropped?
- Decide the concrete database representation for provider-scoped `(iss, sub)`
  principals and separate display emails. Google identities must never resolve
  to an existing local/external login merely because an email string matches.
- Multiple clusters in one UI: OAuth cookies are origin-wide, ours are
  cluster-prefixed — fine today (one cluster), revisit if that changes.
