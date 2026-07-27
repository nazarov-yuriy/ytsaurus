# Deployment: docker compose (local testing) and Kubernetes (Helm)

## docker compose — test everything without touching the host (macOS-friendly)

From `iceberg-viewer/`:

```bash
docker compose up --build            # UI: http://localhost:8080/mock/navigation
docker compose run --rm tests        # all backend suites + end-to-end smoke, in a container
docker compose --profile e2e run --rm e2e   # headless-Chromium render check
```

Compose is deliberately an anonymous local-test profile, not an internal
deployment configuration. Its UI port is bound to `127.0.0.1` so Docker does
not expose it on the host's LAN interfaces. Use the authenticated, fail-closed
Helm configuration below for any shared environment.

Services: `postgres` (users/sessions persisted in the `pgdata` volume),
`mock-backend` (built from `docker/mock-backend.Dockerfile`, `/ready`-gated),
`ui` (official `ghcr.io/ytsaurus/ui` image with `deploy/compose/` configs
mounted — same wiring as the chart), plus on-demand `tests` and `e2e` runners.
The backend suites spawn their own servers inside the tests container; the
PostgreSQL persistence suite uses the composed database, and `compose-smoke.py`
then exercises the composed stack end to end.
On Apple Silicon, if an image lacks arm64, prefix commands with
`DOCKER_DEFAULT_PLATFORM=linux/amd64`. Each fresh `e2e` run installs the pinned
Playwright package in its temporary container and therefore needs npm egress.

# Kubernetes deployment (Helm)

Deploys **ytsaurus-ui + the Python mock backend** as one release, for testing the
Iceberg-viewer stack on a cluster. Modeled on the official
`ytsaurus-ui/packages/ui-helm-chart` (same UI image `ghcr.io/ytsaurus/ui`, same
ConfigMap-mounted `clusters-config.json` + `APP_INSTALLATION=custom` config, same
initContainer secret staging) with the mock proxy taking the place of the
`http-proxies-lb` a real YTsaurus exposes.

## Quick start

```bash
helm install iceberg-ui deploy/helm/iceberg-ui-mock \
  --set auth.allowAnonymous=true   # development-only local port-forward
kubectl port-forward svc/iceberg-ui-iceberg-ui-mock-ui 8080:80
# open http://localhost:8080/mock/navigation?path=//home/iceberg/warehouse
helm test iceberg-ui        # runs the in-cluster smoke test (7 checks)
```

The chart deliberately has no deployable authentication default: rendering
without PostgreSQL, an external verifier, or the explicit
`auth.allowAnonymous=true` development opt-in fails. For an internal
deployment, use the authenticated PostgreSQL example below or configure
`auth.ytUpstream`; do not use the quick-start opt-in.

No custom backend image build or push is needed by default: the chart ships
`server.py`/`data.py`/`webjson.py`/`userdb.py` in a ConfigMap and runs them on
the stock `python:3.12-slim` image. Kubernetes still needs registry access to
pull that base image, and the running pod needs PyPI access as described below.
The UI image is pulled from `ghcr.io/ytsaurus/ui` (same as the official chart).

## Layout

- `helm/iceberg-ui-mock/` — the chart:
  - mock backend: Deployment (+ ConfigMap with sources, process-only `/ping`
    liveness and storage-aware `/ready` readiness probes) and
    ClusterIP Service on port 80, so the UI's `proxy` setting is a bare DNS name —
    exactly how the official chart points at `http-proxies-lb.<ns>.svc.cluster.local`.
  - UI: ConfigMap (`clusters-config.json` generated from `.Values.ui.cluster`,
    pointing at the mock Service; `custom/common.js`), generated Secret with
    `yt-interface-secret.json` (empty in auth-none mode, populated with the
    shared robot token in authenticated mode), Deployment mirroring the
    official chart (supervisord command, initContainer copying the secret into
    `/opt/app/secrets`, `APP_INSTALLATION=custom`,
    `YT_AUTH_ALLOW_INSECURE=1`), ClusterIP Service.
  - `templates/tests/test-smoke.yaml` — `helm test` pod: probes the mock
    directly and serves the UI app shell. Anonymous releases also exercise both
    UI boot gates (`cluster-info`, `cluster-params`) and the UI command tunnel;
    authenticated releases exercise backend commands with the secret-backed
    robot and verify that unauthenticated backend access is rejected.
  - `tests/test-auth-render.sh` — source-level `helm template` regression checks
    for explicit anonymous opt-in, automatic strict authentication with
    `auth.ytUpstream`, rejection of published robot/database placeholders, and
    rejection of missing or contradictory authentication settings.
- PostgreSQL user persistence (`postgres.enabled=true`): adds a `postgres:17-alpine`
  Deployment with a PVC, Secret-managed password (`postgres.password` or
  `postgres.existingSecret` with key `password`), and wires `MOCK_PG_DSN` into the
  mock so users, login sessions, the CSRF secret, and audit rows survive pod
  restarts (catalog fixtures stay fake).
  It also selects `authentication: basic`, enables the UI login flow, and makes
  the backend reject missing/unknown credentials. Set
  `ui.cluster.authentication=none` together with `auth.allowAnonymous=true`
  only when an unauthenticated PostgreSQL-backed development mock is
  intentional. No password users are created automatically; provision local
  accounts explicitly with `userdb.py add-user` after PostgreSQL is ready.
- In run-from-ConfigMap mode the container always installs the exact versions
  in `mock-backend-py/requirements.txt` at start (the FastAPI/uvicorn HTTP
  layer plus the optional PG driver), so it needs egress to PyPI; a startup
  probe protects that installation. The Docker image and Compose test runner
  consume the same file, preventing dependency-version drift. Use the baked
  image for air-gapped clusters.
- External authentication (`auth.ytUpstream=https://proxy.yt.example`): users
  not added locally are verified against that real YTsaurus proxy's `/login`
  and provisioned into the configured user store on first success (no password
  material is stored for them). The setting automatically selects
  `authentication: basic`, enables strict backend authentication, and cannot be
  combined with an explicit `ui.cluster.authentication=none`. Supply a unique
  `auth.robotToken` at the same time; authenticated chart rendering rejects the
  published `mock-robot-token` placeholder. Users created explicitly with
  `userdb.py add-user` always authenticate locally and never contact the
  upstream. Without PostgreSQL, these users and their sessions are
  process-local, so the chart rejects `mockBackend.replicaCount` greater than
  one in authenticated mode; PostgreSQL removes that authentication-state
  restriction, while fake table data remains process-local. See docs/auth.md
  "External authentication".
- `docker/mock-backend.Dockerfile` — optional baked image (includes the pinned
  PostgreSQL dependencies).
  Build and push an explicit tag to a registry reachable by the cluster, then
  use that same repository and tag:

  ```bash
  docker build -f deploy/docker/mock-backend.Dockerfile \
    -t registry.example/iceberg-ui-mock-backend:dev .
  docker push registry.example/iceberg-ui-mock-backend:dev
  helm upgrade --install iceberg-ui deploy/helm/iceberg-ui-mock \
    --set mockBackend.sourcesFromConfigMap=false \
    --set mockBackend.image.repository=registry.example/iceberg-ui-mock-backend \
    --set mockBackend.image.tag=dev
  ```
- The chart's `files/` entries are **relative symlinks into `mock-backend-py/`** —
  there is exactly one copy of the backend sources and dependency pins, so
  nothing can drift.
  `helm template`/`install` from the repo and `helm package` both resolve the
  links (the loader logs "Contents of linked file included and used"); a
  packaged `.tgz` is self-contained. Two caveats: don't copy `deploy/helm/`
  out of the repository by itself (the links would dangle — `helm package` it
  instead), and on Windows clone with `core.symlinks=true`.

## PostgreSQL credentials and rotation

Authenticated chart rendering rejects the published `mock-robot-token`
placeholder, and enabling PostgreSQL rejects the published `mock-password`
placeholder. A pod-level validator also rejects that database placeholder if
it arrives through `postgres.existingSecret`, and the backend performs the
same check before database initialization. Supply unique credentials for both
boundaries:

```bash
helm upgrade --install iceberg-ui deploy/helm/iceberg-ui-mock \
  --set postgres.enabled=true \
  --set postgres.password='a-database-role-password' \
  --set auth.robotToken='a-separate-random-robot-token'
```

An authenticated deployment starts with no local password users. Once the
backend is ready, stream a password from your secret-management workflow into
`kubectl exec -i BACKEND_POD -- python3 /app/userdb.py add-user LOGIN
--password-stdin`; the pod already has `MOCK_PG_DSN` configured. Do not place
the password in the command line, a Helm value, or shell history.
`--password-file <mounted-secret-path>` is also supported. The chart never
enables the anonymous-test `MOCK_ENABLE_DEV_SEED_USERS` fixture.

The same database also receives the backend's audit trail (`audit_log` table:
strict `ts`/`login`/`endpoint`/`http_code` columns plus a schemaless `details` jsonb — see
mock-backend-py/README.md "Audit log"); size it and set a retention policy
before exposing an installation to real traffic.

The database password stays in `PGPASSWORD`; it is not interpolated into a URI.
The backend reconnects after a PostgreSQL connection loss, and `/ready` removes
it from Service endpoints while storage is unavailable. Application-user
passwords are stored as salted PBKDF2-HMAC-SHA256 hashes.

The PostgreSQL container reconciles its role password through its local socket
on every pod start. To rotate the chart-managed credential, change
`postgres.password` in a Helm upgrade. With `postgres.existingSecret`, update
the Secret's `password` key and bump `postgres.existingSecretRevision` in the
same rollout so both PostgreSQL and the backend restart. The `postgres.user`
and `postgres.database` values initialize a new PVC and must not be changed on
an existing one without an explicit database migration.

## Differences from a real YTsaurus deployment

A production YTsaurus is deployed by the ytsaurus-k8s-operator (StatefulSets for
masters/nodes, an `http-proxies-lb` Service for proxies) and the UI by the
official `ui-helm-chart`. This chart is intentionally scoped to the mock backend
and refuses to choose anonymous access implicitly; pointing it at a real proxy
is not a supported production migration. A real deployment also needs correct TLS/`secure`
configuration, `ALLOW_PASSWORD_AUTH` or OAuth, real robot and interface secrets,
and deliberate exposure controls. Use the official UI chart for that setup.

The UI Service is a ClusterIP and ingress is disabled by default. Neither is an
authentication boundary; enabling a NodePort or ingress requires deliberate
authentication, TLS, and network-policy choices.

This remains a development mock: it has no login rate limiting and is not a
production identity service.
