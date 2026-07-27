# Google OAuth for the Iceberg viewer — design notes

Brainstorm, no code changed. Two distinct problems hide in this request:

1. **Sign-in**: authenticate UI users with Google accounts, with an
   *unmodified* ytsaurus-ui.
2. **Delegation**: access GCS-stored Iceberg files *as* those users, not as a
   shared service identity.

They are separable — a sign-in choice does not lock in a delegation choice —
and conflating them is the main design trap here.

## Ground truth (verified in source, this repo)

The "unmodified UI" constraint is much softer than it looks:

- The UI ships a **complete, generic OAuth authorization-code client**:
  `GET /oauth/login` → 302 to `<authPath>?response_type=code&client_id&scope&
  redirect_uri=<origin>/api/oauth/callback&state=…`; the callback exchanges the
  code (with `client_secret`, form-encoded — Google-compatible) and stores
  `yt_oauth_access_token` / `yt_oauth_refresh_token` cookies
  (`ui/src/server/components/oauth.ts`, docs/auth.md §3.4). Enabled purely by
  Node config `ytOAuthSettings {baseURL, authPath, tokenPath, clientId,
  clientSecret, scope}`; the login page shows the SSO button whenever
  `allowOAuth` is true (`LoginFormPage.tsx:151-164`).
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
- `/auth/whoami` stays the single gate, and our CSRF is our own HMAC — both
  work identically for any authentication source. The boot-path robot requests
  (`cluster-params`) keep using the robot token and are unaffected.

## Option A — UI-native OAuth straight against Google

`ytOAuthSettings` with absolute Google URLs, `scope: "openid email"`.
Backend adds one authenticate() branch: `access_token` cookie → validate
against Google → map `email` to a user, auto-provision like `external_login`
(origin `google`), attach identity.

Token validation choices (per request, so caching matters):

| method | cost | notes |
|---|---|---|
| `GET openidconnect.googleapis.com/v1/userinfo` (Bearer) | 1 RTT | what the real proxy's oauth_service does; simplest |
| `GET oauth2.googleapis.com/tokeninfo?access_token=…` | 1 RTT | also returns `aud` — **must check `aud == our clientId`**, otherwise any Google app's token for the victim logs in (token-substitution attack); userinfo alone does not prove audience |
| id_token via JWKS | 0 RTT | offline + fast, but the UI stores the *access* token cookie, not the id_token — not reachable without code changes |

Cache verdicts keyed by token hash with TTL ≤ token expiry; never *store* the
token (it is a bearer credential — the audit sanitizer already refuses to).

Verdict: smallest possible delta, real Google login, but **1-hour sessions
with a manual re-click** (after expiry the UI 401s → login page → SSO button →
silent redirect through Google → new token). Fine for a viewer; annoying for
long sessions. No refresh token also means no long-lived GCS delegation from
this flow.

## Option B — an IdP broker (Dex / Keycloak) between UI and Google

UI does OAuth against the broker (whose token responses match the UI's
Keycloak-flavored expectations, including working refresh); the broker
federates "Log in with Google". Backend validates broker-issued JWTs offline
via JWKS — no Google round-trip per request.

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
                                             ├── /google/login    → mock backend: 302 to Google (code flow,
                                             │                      access_type=offline — WE build this URL)
                                             └── /google/callback → mock backend: exchange code, verify id_token,
                                                                    provision user (origin=google), mint session,
                                                                    Set-Cookie: mock_YTCypressCookie=<64-hex>; Path=/
                                                                    → 302 /mock
```

- No UI change, no YT change; nginx only adds two same-origin routes (in the
  Helm chart this is two extra `location`s on the ingress, or a tiny sidecar).
- The backend already has everything else: session mint (`external_login`
  pattern), whoami, CSRF, audit (login outcomes incl. `origin=google`).
- Because **we** build the authorize URL, `access_type=offline&prompt=consent`
  is available → refresh tokens → long sessions *and* durable GCS delegation.
  Our session cookie TTL (30 d) decouples UI session length from Google token
  lifetime entirely.
- Entry UX: unauthenticated users must land on `/google/login`. Either an
  nginx `auth_request` gate on `/` (redirect when the backend says
  no-session), or just tell users the URL / bookmark — the plain UI 401 page
  would otherwise show the (useless) password form, since we cannot add a
  button to it without modifying the UI. This is the one cosmetic wart.
- Cookie details to honor: `Path=/`, `HttpOnly`, `Secure` (Google mandates
  https redirect URIs except on localhost), **no SameSite** (parity), and the
  cluster-prefixed name must match the cluster id in `clusters-config.json`.

Verdict: **best effort-to-payoff for us**, precisely because it moves the
OAuth client out of the UI (whose client is the limiting factor) into code we
own — while the UI still runs unmodified.

### C2 — the GCP-managed variant: Identity-Aware Proxy

Since deployment is heading to GCP anyway: put IAP in front of the whole UI.
Google handles login/session; every request arrives with a signed
`X-Goog-IAP-JWT-Assertion`; a backend branch verifies it (JWKS, audience) and
auto-provisions the session — no OAuth client code anywhere, no login page at
all. Caveats: ties the deployment to GCP LB/IAP, and IAP proves *identity*
only — it does not hand us a GCS-scoped user token, so delegation still needs
one of the mechanisms below.

## The delegation problem (GCS access on behalf of the user)

What we ultimately need: when `data.py` becomes an Iceberg catalog reader,
its GCS reads should be authorized as the signed-in user. Mechanisms, in
increasing order of trust shifted onto us:

1. **User access token, per session** — request
   `devstorage.read_only` scope at consent; use the token for GCS until it
   expires (~1 h). True GCP IAM enforcement per user; zero storage of secrets.
   Only viable where we control the flow (C1/C2-with-extra-consent; not A).
2. **Refresh tokens stored server-side** (PG, encrypted, revocable) — durable
   on-behalf access; we become a credential store, with all that implies
   (encryption at rest, deletion on user removal, incident blast radius).
   Natural extension of C1; the `users` table already has the right shape for
   an extra opaque column, and the audit trail already attributes every read.
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
  after login), so plain sign-in stays a non-scary `openid email` prompt.
- Tokens expire mid-query: long `read_table` paths need refresh-before-use,
  not refresh-on-401.
- If the catalog side lands on an Iceberg **REST catalog** (e.g. BigLake),
  the REST spec speaks OAuth Bearer natively — the same per-user token can
  flow to the catalog, keeping metadata and data access under one identity.

## Suggested spike order (cheapest disproof first)

1. **Fake-Google spike, no Google account needed**: extend the
   `test_external_auth.py` pattern — an in-process fake issuing codes/tokens
   with Google's response shapes; wire `ytOAuthSettings` at a dev UI against
   it; add the `access_token`-cookie branch to `authenticate()`. Proves the
   full unmodified-UI plumbing (Option A) end-to-end in CI.
2. **Real Google on localhost** (http loopback redirect URIs are allowed):
   confirm consent screen, `aud` validation, the 1-hour expiry UX.
3. Decide A (fastest demo) vs C1 (owns the flow, enables delegation) vs B
   (production shape). A→C1 is not throwaway: the token-validation branch and
   provisioning are shared; C1 adds two routes and the authorize-URL builder.
4. Delegation after sign-in settles; start with mechanism 1 (session-scoped
   user token) and only escalate to stored refresh tokens once the Iceberg
   read path actually exists.

## Open questions / to verify empirically

- Does the UI recover gracefully when `yt_oauth_access_token` expires
  mid-session (expected: 401 → login page with SSO button; confirm no retry
  storm)? What does logout look like with Google (no RP-initiated logout
  endpoint; `logoutPath` unset → cookie-clearing callback only)?
- Express `res.cookie` behavior with `maxAge: NaN` (broker-without-
  `refresh_expires_in` case) — broken cookie or dropped?
- Google `email_verified=false` accounts — reject at provisioning?
- Login collision policy: Google emails vs existing local/external users —
  proposal: emails are a distinct namespace (`origin='google'`, login =
  email), and the no-shadowing rule extends: a Google login must never
  authenticate as an existing `local` user of the same name.
- Multiple clusters in one UI: OAuth cookies are origin-wide, ours are
  cluster-prefixed — fine today (one cluster), revisit if that changes.
