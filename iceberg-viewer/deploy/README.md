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
`server.py`/`data.py`/`webjson.py` in a ConfigMap and runs them on the stock
`python:3.12-slim` image. The UI image is pulled from `ghcr.io/ytsaurus/ui`
(same as the official chart).

## Layout

- `helm/iceberg-ui-mock/` — the chart:
  - mock backend: Deployment (+ ConfigMap with sources, `/ping` probes) and
    ClusterIP Service on port 80, so the UI's `proxy` setting is a bare DNS name —
    exactly how the official chart points at `http-proxies-lb.<ns>.svc.cluster.local`.
  - UI: ConfigMap (`clusters-config.json` generated from `.Values.ui.cluster`,
    pointing at the mock Service; `custom/common.js`), generated Secret with an
    empty `yt-interface-secret.json` (required to be loadable at boot even in
    auth-none mode), Deployment mirroring the official chart (supervisord
    command, initContainer copying the secret into `/opt/app/secrets`,
    `APP_INSTALLATION=custom`, `YT_AUTH_ALLOW_INSECURE=1`), NodePort Service.
  - `templates/tests/test-smoke.yaml` — `helm test` pod: probes the mock
    directly, both UI boot gates (`cluster-info`, `cluster-params`), an `exists`
    command through the UI tunnel, and a `read_table` web_json response.
- `docker/mock-backend.Dockerfile` — optional baked image; set
  `mockBackend.image.repository` and `mockBackend.sourcesFromConfigMap=false`
  to use it.
- `sync-chart-files.sh` — copies `mock-backend-py/*.py` into the chart's
  `files/`; `--check` fails if they drifted (run it after backend changes).

## Differences from a real YTsaurus deployment

A production YTsaurus is deployed by the ytsaurus-k8s-operator (StatefulSets for
masters/nodes, an `http-proxies-lb` Service for proxies) and the UI by
`ui-helm-chart` pointing `proxy` at that Service with `authentication: basic`.
This chart collapses the whole cluster into the one-pod mock and uses
`authentication: none`; swapping `.Values.ui.cluster` back to a real proxy +
`basic` reproduces the official setup.
