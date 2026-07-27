# Deployment: docker compose (local testing) and Kubernetes (Helm)

## docker compose — test everything without touching the host (macOS-friendly)

From `iceberg-viewer/`:

```bash
docker compose up --build            # UI: http://localhost:8080/mock/navigation
docker compose run --rm tests        # all backend suites + end-to-end smoke, in a container
docker compose --profile e2e run --rm e2e   # headless-Chromium render check
```

Services: `postgres` (users/sessions persisted in the `pgdata` volume),
`mock-backend` (built from `docker/mock-backend.Dockerfile`, `/ready`-gated),
`ui` (official `ghcr.io/ytsaurus/ui` image with `deploy/compose/` configs
mounted — same wiring as the chart), plus on-demand `tests` and `e2e` runners.
The suites are self-contained (they spawn their own servers inside the tests
container); `compose-smoke.py` then exercises the composed stack end to end.
On Apple Silicon, if an image lacks arm64, prefix commands with
`DOCKER_DEFAULT_PLATFORM=linux/amd64`. The `e2e` profile needs npm egress once.

# Kubernetes deployment (Helm)

Deploys **ytsaurus-ui + the Python mock backend** as one release, for testing the
Iceberg-viewer stack on a cluster. Modeled on the official
`ytsaurus-ui/packages/ui-helm-chart` (same UI image `ghcr.io/ytsaurus/ui`, same
ConfigMap-mounted `clusters-config.json` + `APP_INSTALLATION=custom` config, same
initContainer secret staging) with the mock proxy taking the place of the
`http-proxies-lb` a real YTsaurus exposes.

## Quick start

```bash
helm install iceberg-ui deploy/helm/iceberg-ui-mock
kubectl port-forward svc/iceberg-ui-iceberg-ui-mock-ui 8080:80
# open http://localhost:8080/mock/navigation?path=//home/iceberg/warehouse
helm test iceberg-ui        # runs the in-cluster smoke test (7 checks)
```

No registry access is needed for the backend: by default the chart ships
`server.py`/`data.py`/`webjson.py`/`userdb.py` in a ConfigMap and runs them on
the stock `python:3.12-slim` image. The UI image is pulled from
`ghcr.io/ytsaurus/ui` (same as the official chart).

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
    for anonymous defaults, automatic strict authentication with
    `auth.ytUpstream`, rejection of the published robot-token placeholder in
    authenticated mode, and rejection of contradictory authentication settings.
- PostgreSQL user persistence (`postgres.enabled=true`): adds a `postgres:17-alpine`
  Deployment with a PVC, Secret-managed password (`postgres.password` or
  `postgres.existingSecret` with key `password`), and wires `MOCK_PG_DSN` into the
  mock so users and login sessions survive pod restarts (table data stays fake).
  It also selects `authentication: basic`, enables the UI login flow, and makes
  the backend reject missing/unknown credentials. Set
  `ui.cluster.authentication=none` explicitly only when an unauthenticated
  PostgreSQL-backed mock is intentional. No password users are created
  automatically; provision local accounts explicitly with `userdb.py add-user`
  after PostgreSQL is ready.
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
  upstream. See docs/auth.md "External authentication".
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
placeholder. Supply a unique robot token; for PostgreSQL-backed authentication,
replace the database password at the same time:

```bash
helm upgrade --install iceberg-ui deploy/helm/iceberg-ui-mock \
  --set postgres.enabled=true \
  --set postgres.password='a-database-role-password' \
  --set auth.robotToken='a-separate-random-robot-token'
```

An authenticated deployment starts with no local password users. Once the
backend is ready, run `/app/userdb.py add-user <login> <password>` in a backend
pod using your normal secret-injection process; the pod already has
`MOCK_PG_DSN` configured. The chart never enables the anonymous-test
`MOCK_ENABLE_DEV_SEED_USERS` fixture.

The same database also receives the backend's audit trail (`audit_log` table:
strict `ts`/`login`/`endpoint` columns plus a schemaless `details` jsonb — see
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
and uses `authentication: none`; pointing it at a real proxy is not a supported
production migration. A real deployment also needs correct TLS/`secure`
configuration, `ALLOW_PASSWORD_AUTH` or OAuth, real robot and interface secrets,
and deliberate exposure controls. Use the official UI chart for that setup.

The UI Service is a ClusterIP and ingress is disabled by default. Because the
mock is unauthenticated in the default configuration, enabling a NodePort or
ingress should be an explicit decision.

This remains a development mock: it has no login rate limiting and is not a
production identity service.
