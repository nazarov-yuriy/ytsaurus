# Architecture review: does the mock match YTsaurus' high-level design?

**Short answer:** the real stack has three tiers, and this implementation
matches them: the mock occupies the HTTP-proxy tier as a single data-role proxy.
The accepted simplifications are listed below.

## 1. The real YTsaurus design (verified in this repo)

There are **three** backend tiers on the UI's request path, not two:

```
browser (React SPA + javascript-wrapper)
   │  /api/cluster-info, /api/settings/…, /api/yt/<cluster>/api/v3/<cmd>, …
   ▼
① UI's own Node/Express backend            (ytsaurus-ui packages/ui/src/server)
   │  /api/v3|v4/<cmd>, /auth/whoami, /login, /hosts
   ▼
② YTsaurus HTTP proxy                      (yt/yt/server/http_proxy, C++)
   │  native protocols
   ▼
③ cluster internals (masters, nodes, scheduler) — plus a separate
   RPC proxy interface (port 9013) that the UI never uses
```

- **Tier ① — the "UI API" backend.** The UI ships its own server that serves
  the SPA, UI-specific endpoints (cluster-info/params, settings, presets,
  markdown, …) and reverse-proxies all YT commands to the HTTP proxy
  (`yt-api.ts`). This is a real, separate backend type — our API catalog
  tracks it as the `ui-server` layer (109 endpoints total across both layers).
- **Tier ② — HTTP proxies with roles.** Confirmed in
  `yt/docs/en/_includes/user-guide/proxy/about.md` and the proxy source:
  every HTTP proxy carries a **role**; Cypress requests (`get`, `set`, …) go to
  **`control`** proxies, while heavy data commands (`read_table`,
  `write_table`, …) go to **`data`** proxies ("heavy proxies"). Mechanics
  verified in code:
  - `/hosts` returns live proxies filtered by role; the default filter is the
    **`data`** role (`coordinator.cpp:551`, `config.cpp:65`,
    `NApi::DefaultHttpProxyRole = "data"`, `client/api/public.h:237`), ordered
    by load (fitness), per the docs' "re-query /hosts and rotate" guidance.
  - A **control**-role proxy refuses/deflects heavy commands: with input data it
    errors ("Control proxy may not serve heavy requests with input data"),
    otherwise it **redirects to a data proxy** — *unless* the client sends
    `X-YT-Suppress-Redirect` or is a browser (`context.cpp:291-305`,
    `CanHandleHeavyRequests() == role != "control"`, `coordinator.cpp:215`).
  - A **data**-role proxy serves *everything* — heavy commands and Cypress
    commands alike. The role split is a load-isolation policy, not a
    capability split.
- **Tier ③ / RPC proxies** are irrelevant to the UI: the UI stack speaks only
  HTTP-proxy protocol (and `internal/discover_versions` for system pages).

## 2. What our stack does

| Design element | Our implementation | Verdict |
|---|---|---|
| Tier ① UI API backend | The **real** ytsaurus-ui Node server, unmodified (dev mode or the official image in the chart) | ✅ matches by construction |
| Tier ② HTTP proxy | `mock-backend{,-py}` implements the HTTP-proxy protocol: `/api/v3|v4`, `/auth/whoami`, `/login`, `/hosts`, `/ping`, `/version` | ✅ correct tier; the mock replaces *only* the proxy |
| Proxy roles (control vs data) | One mock instance serves both Cypress and heavy commands; `/hosts` returns itself | ✅ *as a degenerate case*: identical to a one-proxy cluster whose single proxy has the `data` role (data proxies legitimately serve everything) |
| Heavy-proxy discovery | `disableHeavyProxies: true` in our clusters-config; browser wrapper has `useHeavyProxy=false` hardcoded; UI tunnel always sends `X-YT-Suppress-Redirect: 1` | ✅ we route everything to one address exactly the way the UI itself does against real clusters |
| Redirect/refusal on control proxies | Not implemented | ✅ unreachable in the UI topology (suppress-redirect always set, browser requests exempt anyway) — see gaps |
| RPC proxy | Not implemented | ✅ by design; UI never uses it |

## 3. Known simplifications inside the proxy tier (accepted, documented)

1. ~~`/hosts` ignores the `?role=` query parameter~~ **Fixed:** `/hosts` now
   filters by role (`data`/default → `[self]`, others → `[]`), matching
   coordinator.cpp with the mock as a single data-role proxy. Load-ordered
   multi-proxy fitness remains out of scope (one instance).
2. **No role attribute, no control-role behavior**: the mock never redirects or
   refuses heavy commands. Reachable only by non-UI clients that omit
   `X-YT-Suppress-Redirect`.
3. **`/hosts/all` returns `[]`** instead of per-proxy objects (System page
   feature, out of viewer scope).
4. **No proxy heartbeat/liveness model** (`//sys/http_proxies` registration,
   banning, fitness) — meaningless for a single mock instance.
5. `internal/discover_versions/v2` is stubbed out of scope (Components page).

## 4. Optional fidelity plan (if ever needed — not now)

Ordered by value:

1. ~~Role-aware `/hosts`~~ — done (see §3.1).
2. **Two-instance mode in the Helm chart**: a `control`-role and a `data`-role
   mock Deployment plus role-filtering `/hosts`, to rehearse the real
   topology (`http-proxies-lb` → control, heavy redirect → data). Only worth it
   if SDK/CLI clients (not just the UI) will ever point at the mock.
3. **Control-proxy semantics**: implement the `context.cpp:291-305` behavior
   (307 redirect to a data proxy / "may not serve heavy requests" error) behind
   a `MOCK_ROLE=control` env, with dual-backend tests.
4. **`/hosts/all` object shape** for the System→Proxies page.

A real Iceberg backend serving only this UI can stay a single "data-role"
endpoint indefinitely; the role split matters only under load isolation, which
a viewer-scale deployment does not need.
