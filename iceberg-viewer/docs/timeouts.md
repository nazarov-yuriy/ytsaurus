# Timeouts on every layer (and simulating a slow catalog)

A slow Iceberg catalog (1–30s per listing/read) must fit inside every timeout on
the request path. This documents each layer, verified against code, and the
`MOCK_DELAY` switch the mock backend provides for testing it.

## The request path and its timeouts

```
browser (jsw wrapper) ──► [dev: Rspack proxy | prod: nginx] ──► UI node server ──► mock/proxy
```

| # | Layer | Timeout | Source |
|---|-------|---------|--------|
| 1 | Browser: javascript-wrapper axios, all commands | **100 s** | `jsw/lib/utils/setup.js:118` (`timeout` option default 100000) |
| 1a | Browser: `bigUpload` commands only | 24 h | `jsw/lib/core.js:269` |
| 2a | Dev only: Rspack/webpack-dev-server proxy 8080→8081 | none (OS-level) | http-proxy defaults, no `proxyTimeout` configured |
| 2b | Prod only: nginx in the `ghcr.io/ytsaurus/ui` image | **100 s** read/send/connect, keep-alive 100 s | `packages/ui/deploy/nginx/nginx.conf:21-23` |
| 3 | UI node server as an HTTP server (Node defaults) | headers 60 s, request receive 300 s, idle keep-alive 5 s | Node `http` defaults |
| 4 | UI node server → proxy: the `/api/yt/…` tunnel | **100 s** | `src/server/controllers/yt-api.ts:123` (`timeout: 100000`) |
| 5 | UI node server → proxy: `/auth/whoami` (cluster-info) | **15 s** | `src/server/components/cluster-queries.ts:14` |
| 6 | UI node server → proxy: `/version` (cluster-info) | **5 s** | `cluster-queries.ts:77` |
| 7 | UI node server → proxy: robot batches for cluster-params (`//sys/media`, `@ui_config`, …) | **5 s** | `src/server/components/requestsSetup.ts:61` |
| 8 | Mock backend: per request | none (a handler may take arbitrarily long) | `mock-backend-py/server.py` |
| 9 | Mock backend: idle keep-alive between requests | 5 s (uvicorn `timeout_keep_alive`) | `server.py` entrypoint |
| 10 | Mock (python) → PostgreSQL | connect 5 s (chart DSN `connect_timeout=5`); no statement timeout | chart `mock-backend.yaml` |
| 10a | Mock (python) → external YTsaurus `/login` (`MOCK_YT_UPSTREAM`) | **5 s** default, `MOCK_YT_UPSTREAM_TIMEOUT` | `server.py upstream_login` |
| 11 | k8s probes (chart): liveness/readiness `/ping`, `/ready` | 1 s per attempt (k8s default `timeoutSeconds`), period 10 s | `deploy/helm/.../mock-backend.yaml` |
| 12 | k8s startup probe (pip-install window, PG mode) | up to 300 s (60×5 s) | `mock-backend.yaml` startupProbe |
| 13 | `helm test` smoke pod curls | none explicit (curl defaults) | `templates/tests/test-smoke.yaml` |

## What this means for slow table access

- **The end-to-end budget for data commands is 100 s** — the browser wrapper (1),
  nginx in production (2b), and the UI tunnel (4) all agree on 100 s. Anything
  slower than ~100 s fails at three layers at once; 1–30 s fits comfortably
  (verified: a 30 s `read_table` completes through the full chain).
- **The boot path must stay fast.** `cluster-params` robot batches against
  `//sys/…` run with a 5 s timeout (7) and a failing `mediumList` blocks the
  whole cluster page; `/version` has 5 s (6) and `/auth/whoami` 15 s (5). So a
  slow catalog implementation must keep control-plane reads (`//sys`,
  `/version`, `/auth/whoami`, `/ping`, `/ready`) off the slow path.
- **Probes are on a separate 1 s budget** (11): `/ping` (process alive) and
  `/ready` (storage reachable) must never touch table data. In the mock it
  doesn't, and slow requests run on other threads, so probes stay fast during
  slow reads (covered by tests).
- The UI shows its usual loading spinners while requests are in flight; nothing
  client-side gives up before 100 s.

## Simulating slowness: MOCK_DELAY

The backend accepts `MOCK_DELAY`:

```bash
MOCK_DELAY=1500 python3 mock-backend-py/server.py 8000  # all data commands +1.5s
MOCK_DELAY=read_table:5000,list:2000,get:1000 \
  python3 mock-backend-py/server.py 8000                # per command
```

- Applies to `get`/`list`/`exists`/`read_table`, both top-level and per
  sub-command inside `execute_batch`.
- **`//sys` paths are never delayed** (rule follows from the 5 s boot budget
  above); infrastructure endpoints (`/ping`, `/ready`, `/version`,
  `/auth/whoami`, `/login`) are never delayed.
- Sync command handlers run in FastAPI's bounded worker pool, so ordinary
  requests can overlap. `/ping` stays on the event loop; readiness health
  checks and audit persistence use separate bounded executors and stay
  independent of that pool.
- Helm: `--set mockBackend.delay=read_table:5000,list:2000`.

Covered by `tests/test_slow_backend.py` (9 tests: delayed-and-correct responses,
`//sys` and infrastructure exemptions, per-sub-command batch delays, concurrent
fast paths, `/ping` under a saturated handler pool or stalled audit write, and
bounded readiness waiters).
