# Running the YTsaurus UI against a mock backend

Everything below was verified against the sources in this workspace:

- UI: `/shared/ytsaurus4/iceberg-viewer/ytsaurus-ui` (monorepo, app in `packages/ui`)
- Proxy semantics: `/shared/ytsaurus4/yt/yt/server/http_proxy`
- Existing mock: `/shared/ytsaurus4/iceberg-viewer/mock-backend/server.js`

File references are `path:line` relative to those roots.

---

## 0. TL;DR — from zero to a rendered Navigation page

```bash
# 0. mock proxy on :8000 (must answer /version, /auth/whoami and /api/v3|v4/*;
#    /hosts is needed only when heavy-proxy discovery is enabled)
node /shared/ytsaurus4/iceberg-viewer/mock-backend/server.js 8000

# 1. deps (node >= 24 per packages/ui/package.json:"engines")
cd /shared/ytsaurus4/iceberg-viewer/ytsaurus-ui
npm ci                       # root postinstall runs `lerna run deps:install` -> `npm ci` in packages/ui

# 2. cluster config
cd packages/ui
cat > clusters-config.json <<'JSON'
{
  "clusters": [
    {
      "id": "mock",
      "name": "Mock cluster",
      "proxy": "localhost:8000",
      "secure": false,
      "authentication": "none",
      "theme": "grapefruit",
      "description": "Local mock proxy",
      "environment": "development",
      "group": "Mock",
      "disableHeavyProxies": true
    }
  ]
}
JSON

# 3. stock common config requires this file at boot; no token is needed in "none" mode
mkdir -p secrets
printf '{}\n' > secrets/yt-interface-secret.json

# 4. run: dev-server on 8080, node server on 8081 (8080 proxies to 8081)
LOCAL_DEV_PORT=8080 npm run dev:app

# 5. open http://localhost:8080/mock/navigation?path=//
```

With `authentication: "none"` the file needs no token data, but the stock common
config still requires the path to be loadable at boot. The empty object above is
sufficient. An installation config can instead set `ytInterfaceSecret:
undefined` (see §2.4).

---

## 1. `clusters-config.json`

### 1.1 Where it is loaded

| What | Where |
|---|---|
| Default path | `packages/ui/src/server/configs/common.ts:52` — `clustersConfigPath: path.resolve(__dirname, '../../../clusters-config.json')`. Relative to the compiled `dist/server/configs`, i.e. **`<app root>/clusters-config.json`** (repo root of `packages/ui` in dev, `/opt/app` in docker). |
| Loaded (validated) at boot | `packages/ui/src/server/configure-app.ts:14-18` — plain `require(app.config.clustersConfigPath)`, so it must be valid JSON (or JS) and is cached by `require` — **a config change needs a server restart**. |
| Parsed into a map | `packages/ui/src/server/config.realcluster.ts:5-23` — `getRealClustersConfig()` reads `{clusters: [...]}` and re-keys the array by `item.id`. Missing `clusters` key throws `Please make sure you have provided correct file <path>`. |
| Chosen vs. local-mode config | `packages/ui/src/server/components/utils.ts:15-23` — if `APP_ENV=local` (`utils/index.ts:22-24`) or `ytAllowRemoteLocalProxy` + a proxy param, the synthetic local-mode config in `config.localcluster.ts` is used **instead of** the file. |
| Pre-start guard | `packages/ui/scripts/check-start-files.sh:1-9` — `npm run dev` aborts unless `APP_ENV=local` or `./clusters-config.json` exists. |
| Example | `packages/ui/clusters-config.json.example` |

`clustersConfigPath` can be overridden by an installation config
(`APP_INSTALLATION`, see §2.2) — there is **no** `CLUSTERS_CONFIG_PATH`
environment variable in the OSS code base.

### 1.2 Full schema of one entry

Source of truth: `packages/ui/src/shared/yt-types.d.ts:100-150`.

| Field | Type | Req. | Meaning / effect |
|---|---|---|---|
| `id` | `string` | ✅ | Key of the cluster; first URL segment (`/<id>/navigation`), used in `/api/**/:ytAuthCluster` routes and in the XSRF cookie name. |
| `name` | `string` | ✅ | Display name in the cluster menu. |
| `proxy` | `string` | ✅ | `host[:port]` of the HTTP proxy — **no scheme**. Scheme is derived from `secure` (`components/requestsSetup.ts:64`). |
| `theme` | `ClusterTheme` | ✅ | One of the enum at `yt-types.d.ts:78-98`: `grapefruit, bittersweet, sunflower, grass, mint, aqua, bluejeans, lavander, pinkrose, lightgray, mediumgray, darkgray, dornyellow, rubber, electricviolet`. |
| `environment` | `'development' \| 'production' \| 'prestable' \| 'testing' \| 'localmode'` | ✅ | Badge/colouring only. |
| `secure` | `boolean` | ➖ | `true` → `https://`, `false`/absent → `http://` (`requestsSetup.ts:51,64`). **Must be `false` for `http://localhost:8000`.** |
| `authentication` | `'none' \| 'basic' \| 'domain'` | ➖ | See §1.3. Default `'none'` (`requestsSetup.ts:57`). |
| `description` | `string` | ➖ | Tooltip in cluster menu. |
| `group` | `string` | ➖ | Groups clusters in the menu (`src/ui/config/yt-config.ts:9-30`); falls back to `DEFAULT_GROUP`. |
| `primaryMaster.cellTag` | `number` | ➖ | Displayed on the System page; purely informational for the UI. |
| `infra` | `{preset, serviceId, environmentId, dataCenters?}` | ➖ | Yandex-internal infra widget; ignore in OSS. |
| `externalProxy` | `string` | ➖ | Host used **from the browser** instead of `proxy` for direct heavy URLs (download/upload/job stderr) — `src/ui/utils/navigation/index.ts:148-166`. |
| `hwOrder` | `unknown` | ➖ | Ordering hint, unused in OSS. |
| `urls` | `{icon, icon2x, iconbig?}` | ➖ | Cluster tile images. |
| `loginPageSettings` | `{title?, text?}` | ➖ | HTML overrides on the password-login page. |
| `operationPageSettings` | `{disableOptimizationForYTFRONT2838: boolean}` | ➖ | Operation page tweak. |
| `disableHeavyProxies` | `boolean` | ➖ | **Key for mocks.** `true` → the node server never calls `GET /hosts` for heavy commands (`controllers/yt-api.ts:92`). |
| `uiSettings` | `Partial<Pick<UISettings,'uploadTableExcelBaseUrl'\|'exportTableBaseurl'>>` | ➖ | Per-cluster ui-setting overrides. |

### 1.3 `authentication` semantics

Interpreted in two places:

- **Node server → proxy** (`packages/ui/src/server/components/requestsSetup.ts:84-132`):
  - `'none'` → **no** auth headers are attached at all, and `getRobotYTApiSetup` does not require `ytInterfaceSecret`.
  - `'basic' | 'domain'` → `Authorization: OAuth <token>` for robot requests (token from `secrets/yt-interface-secret.json` or `YT_TOKEN`, `requestsSetup.ts:26-27`), and `req.yt.ytApiAuthHeaders` (a `Cookie: YTCypressCookie=…` built in `middlewares/yt-auth.ts:16-20`) for user requests.
- **Browser → wrapper** (`packages/ui/src/ui/common/yt-api.ts:39`): `yt.setup.setGlobalOption('authentication', {type: config.authentication || 'none'})`. In the wrapper, `withCredentials` is enabled only when the type is set and ≠ `'none'` (`node_modules/@ytsaurus/javascript-wrapper/lib/core.js:246`).

**For a mock, use `"authentication": "none"`.** That is the only value that
needs no robot token value and no login round-trip. The stock server config
still requires a loadable (possibly empty) secret file at boot (§2.4). Note it
is orthogonal to
`ALLOW_PASSWORD_AUTH`: password auth is a *server-wide* switch (§2.2), and
when it is off, `GET /api/clusters/auth-status` reports every cluster as
`{authorized: true}` (`controllers/clusters.ts:31-39`).

### 1.4 Ready-to-use mock config

`packages/ui/clusters-config.json`:

```json
{
  "clusters": [
    {
      "id": "mock",
      "name": "Mock cluster",
      "proxy": "localhost:8000",
      "secure": false,
      "authentication": "none",
      "theme": "grapefruit",
      "description": "Local mock proxy",
      "environment": "development",
      "group": "Mock",
      "primaryMaster": {"cellTag": 1},
      "disableHeavyProxies": true
    }
  ]
}
```

`disableHeavyProxies: true` is optional if your mock implements `/hosts`
correctly (§6), but it removes one round-trip per heavy command.

---

## 2. Server configuration & environment

### 2.1 Config layering

`packages/ui/src/server/index.ts:15` creates NodeKit with
`configsPath = <dist>/server/configs`. NodeKit merges, in order
(`packages/ui/docs/configuration.md:1-10`, nodekit `dist/nodekit.js:48-59`):

1. `configs/common.(js|ts)` — always
2. `configs/{APP_ENV}.js` — if `APP_ENV` is set
3. `configs/{APP_INSTALLATION}/common.js` — if `APP_INSTALLATION` is set
4. `configs/{APP_INSTALLATION}/{APP_ENV}.js`

Shipped configs: `src/server/configs/common.ts`, `src/server/configs/local.ts`
(`APP_ENV=local`), `src/server/configs/e2e/local.ts`
(`APP_INSTALLATION=e2e APP_ENV=local`).

`applyAppEnvToConfig` (`src/server/utils/configs/apply-app-env-to-config.ts:43-49`)
then folds a handful of environment variables into the config.

### 2.2 Environment variables that matter

| Var | Read at | Effect |
|---|---|---|
| `APP_ENV` | nodekit `dist/nodekit.js:49`; `src/server/utils/index.ts:22` | Config layer name. **`APP_ENV=local` switches the whole app into "local cluster" mode**, which ignores `clusters-config.json` (`components/utils.ts:18`) and synthesises clusters from `PROXY`. `npm run dev:app` defaults it to `development`. |
| `APP_INSTALLATION` | nodekit `dist/nodekit.js:48` | Extra config directory (`oss`, `e2e`, …). |
| `APP_DEV_MODE` | nodekit `dist/nodekit.js:50` | Source maps (`configure-app.ts:20-23`). Set to `1` by `npm run dev:app`. |
| `APP_PORT` | expresskit `dist/expresskit.js:21` | TCP port for the node server. If unset, it listens on the unix socket `dist/run/server.sock` (`configs/common.ts:11`). |
| `APP_SOCKET` | expresskit `dist/expresskit.js:20` | Override the socket path. |
| `LOCAL_DEV_PORT` | `packages/ui/build.app.config.ts:15,35-53` | Dev only: client dev-server port = `LOCAL_DEV_PORT`, node server port = `LOCAL_DEV_PORT + 1` (passed on as `APP_PORT`, app-builder `dist/commands/dev/index.js:61-63`); the dev-server proxies everything but `/build` to the node server (`dist/commands/dev/client.js:153-172`). **Open the browser on `LOCAL_DEV_PORT`.** |
| `PROXY` | `src/server/config.localcluster.ts:5` | Local-mode proxy `host:port`. Only used when `APP_ENV=local`. |
| `PROXY_INTERNAL` | `src/server/components/utils.ts:62-69` | Overrides `proxy` **only** for the cluster whose id equals `YT_LOCAL_CLUSTER_ID`. |
| `YT_LOCAL_CLUSTER_ID` | `src/server/constants/index.ts:1` | Id of the synthetic local cluster (default `ui`). |
| `ALLOW_PASSWORD_AUTH` / `WITH_AUTH` / `YT_AUTH_CLUSTER_ID` | `utils/configs/apply-app-env-to-config.ts:30-40` | Any non-empty value enables YT password auth. `YT_AUTH_CLUSTER_ID` is a legacy enablement signal only; it no longer selects an auth cluster. Leave all three **unset** for a no-auth mock. |
| `YT_AUTH_ALLOW_INSECURE` | same, `:36` | Strips `Secure` from the `YTCypressCookie` when the origin is `http://` (`controllers/login.ts:89-118`). Needed only with password auth over plain HTTP. |
| `YT_TOKEN` | `src/server/components/requestsSetup.ts:27` | Fallback robot OAuth token when `secrets/yt-interface-secret.json` is absent. |
| `PROMETHEUS_BASE_URL` | `apply-app-env-to-config.ts:23` | Enables monitoring dashboards + `allowPrometheusDashboards`. |
| `GRAFANA_BASE_URL`, `YT_DOCS_BASE_URL`, `YTFRONT_UPLOAD_EXCEL_BASE_URL`, `YTFRONT_EXPORT_EXCEL_BASE_URL`, `YTFRONT_JUPYTER_BASE_URL` | `apply-app-env-to-config.ts:11-15` | `uiSettings` overrides. |
| `DEBUG_PORT` | `build.app.config.ts:10` | `--inspect-brk` port for the node server. |
| `NODE_OPTIONS=--max-http-header-size=204800` | `package.json` scripts | Required: YT parameters travel in `X-YT-Parameters-*` headers up to 64 KiB × 2. |

The old `config.ytAuthCluster` key is **removed**; if it is present the server
serves a 500 error page instead of the app
(`middlewares/check-configuration.ts:18-35`). The similarly named
`YT_AUTH_CLUSTER_ID` environment variable remains only as the legacy
password-auth signal described above.

### 2.3 Configuration keys you may want for a mock

Defined in `packages/ui/src/@types/core.d.ts:11-146`:

- `clustersConfigPath` (`:26`) — path to the cluster file.
- `ytInterfaceSecret` (`:21`) — robot token file. Its token contents are unused
  for `authentication: 'none'`, but the stock common config sets the path and
  checks that it is loadable at boot; use an empty `{}` file or explicitly set
  this key to `undefined` (§2.4).
- `userSettingsConfig` (`:65-69`) — when **absent**, user settings fall back to `localStorage` (§5.4). Leave unset for a mock.
- `userColumnPresets` (`:78-81`) — needs a dynamic table; leave unset.
- `ytApiUseCORS` (`:32`) — `false`/absent (default) routes ordinary wrapper YT
  API calls through the node server at `/api/yt/<cluster>/…`. Direct downloads
  and uploads are separate exceptions (§5.5).
- `uiSettings.directDownload` — `true` in `configs/common.ts:56`. This makes *downloads* go straight from the browser to `//<externalProxy ?? proxy>/api/v3/<cmd>` (`src/ui/utils/navigation/index.ts:163-165`), which **does** need CORS on the mock. Set it to `false` to funnel downloads through the node server too.
- `ytAllowRemoteLocalProxy` (`:37`) — allows arbitrary `host:port` cluster ids; not needed.

Override them with an installation config, e.g. `APP_INSTALLATION=mock` and
`src/server/configs/mock/common.ts`:

```ts
import {type AppConfig} from '@gravity-ui/nodekit';
const config: Partial<AppConfig> = {
    ytInterfaceSecret: undefined,
    userSettingsConfig: undefined,
    uiSettings: {directDownload: false},
};
export default config;
```

### 2.4 Secrets file

`secrets/yt-interface-secret.json` (`configs/common.ts:51`) is only *read*
lazily by `getRobotSecret` (`components/requestsSetup.ts:8-24`), but
`configure-app.ts:8-12` `require()`s it at boot **if the config key is set**.
`configs/common.ts` always sets it, so with the stock config the file must
exist. Two ways out:

- create a stub `packages/ui/secrets/yt-interface-secret.json` containing `{}`, or
- set `ytInterfaceSecret: undefined` in an installation config (that is what `configs/local.ts:9` and `configs/e2e/local.ts:5` do).

---

## 3. Running in dev mode

```bash
cd /shared/ytsaurus4/iceberg-viewer/ytsaurus-ui
npm ci                                   # root; postinstall -> lerna run deps:install
cd packages/ui
# clusters-config.json must exist (scripts/check-start-files.sh:1-9)
mkdir -p secrets && echo '{}' > secrets/yt-interface-secret.json

LOCAL_DEV_PORT=8080 npm run dev:app
# -> client dev server  http://localhost:8080   (open this)
# -> node server        http://localhost:8081
```

`npm run dev:app` expands to (`packages/ui/package.json`, `scripts.dev:app`):

```
./scripts/check-start-files.sh && npm run copy:icons &&
APP_ENV=${APP_ENV:-development} APP_DEV_MODE=1 \
NODE_OPTIONS="--max-http-header-size=204800 ${NODE_OPTIONS}" \
app-builder dev --config ./build.app.config.ts
```

Variants:

| Script | What it does |
|---|---|
| `npm run dev` | alias for `dev:app` |
| `npm run dev:oss` | `APP_INSTALLATION=oss npm run dev` |
| `npm run dev:localmode` | sources `scripts/dev.localmode-env.sh` (interactive; sets `APP_ENV=local`, `PROXY=<host>:8000`, `YT_LOCAL_CLUSTER_ID=ui`) and can spin up a real local YTsaurus in docker. **Not** what you want for a mock — local mode bypasses `clusters-config.json`. |
| `npm run debug` | `app-builder dev --inspect` |
| `npm run build && npm start` | production build; `npm start` = `node dist/server`, listening on `APP_PORT` or the unix socket. |

Without `LOCAL_DEV_PORT` the node server binds the unix socket
`dist/run/server.sock` and you need nginx in front — see
`packages/ui/deploy/nginx/yt.development.conf.example` and the README
("Development" section). With `LOCAL_DEV_PORT` nginx is unnecessary.

Notes:

- `packages/ui/package.json` declares `"engines": {"node": ">=24"}`.
- The dev-server serves `/build/*` itself; everything else is proxied to the node server (`@gravity-ui/app-builder/dist/commands/dev/client.js:153-172`). It also opens a **webpack HMR websocket** at `/build/sockjs-node` (`dist/commands/dev/client.js:53`) — the only websocket in the stack; it is dev-only and unrelated to the mock.

### 3.1 Docker

Build (`packages/ui/Dockerfile`):

```bash
cd packages/ui
docker build . -t ytsaurus-ui:my-tag
```

Prebuilt images: `ghcr.io/ytsaurus/ui:<tag>` (stable) and
`ghcr.io/ytsaurus/ui-nightly` (`packages/ui/package.json`, `config.docker_image`).

The image runs supervisord → nginx (`:80`) + `node dist/server` on the unix
socket (`deploy/supervisor/conf.d/app.conf:14-23`,
`deploy/nginx/sites-enabled/app.conf:22-38`). App root is `/opt/app`.
`deploy/scripts/preflight.sh:6` substitutes `APP_HTTP_PORT` (default `80`)
into the nginx config.

Run against a mock proxy on the host:

```bash
docker run --rm -it \
  --network host \
  -e APP_HTTP_PORT=8081 \
  -e NODE_OPTIONS=--max-http-header-size=204800 \
  -v "$PWD/clusters-config.json:/opt/app/clusters-config.json:ro" \
  -v "$PWD/secrets:/opt/app/secrets:ro" \
  ghcr.io/ytsaurus/ui:stable
# open http://localhost:8081/mock/navigation?path=//
```

(Without `--network host`, use `-p 8081:80` and set `proxy` to a host
address the container can reach — `host.docker.internal:8000` on
Docker Desktop, or the docker bridge IP on Linux; `localhost` inside the
container is the container itself.)

The runtime paths come from `configs/common.ts`:
`/opt/app/clusters-config.json` and
`/opt/app/secrets/yt-interface-secret.json`.

For reference, the way the official launcher starts the image
(`/shared/ytsaurus4/yt/docker/local/run_local_cluster.sh:464-478`) uses
**local mode** instead of a cluster file:

```
docker run -itd --network <net> --name yt.frontend -p 8001:80 \
  -e YT_LOCAL_CLUSTER_ID=<cluster> \
  -e PROXY=<docker_hostname>:<proxy_port> \
  -e PROXY_INTERNAL=<yt_container>:80 \
  -e APP_ENV=local \
  -e APP_INSTALLATION=<installation> \
  -e LOCALMODE_EXTERNAL_PROXY=… -e AUTH_COOKIE_DOMAIN=… \
  ghcr.io/ytsaurus/ui:<ver>
```

`LOCALMODE_EXTERNAL_PROXY` and `AUTH_COOKIE_DOMAIN` are **not** consumed by
the OSS server code (no references in `src/server`); they exist for
downstream builds.

---

## 4. Bootstrap: what the browser gets and what it asks for

### 4.1 The HTML shell (`GET /` and `GET /:cluster/…`)

Handled by `homeIndexFactory` (`src/server/controllers/home.ts:12-93`),
mounted on every UI route in `src/server/routes.ts:53-62,127-132`. It:

1. Resolves the cluster config (`home.ts:28`). If the cluster is unknown it either redirects to `/` or renders a 404 explanation (`home.ts:32-44`).
2. Builds the settings blob: server-side defaults (`utils/default-settings.ts:88-91`) plus, when `userSettingsConfig` is configured, the user's document read from Cypress (`home.ts:46-80`).
3. Renders the layout (`components/layout-config.ts:19-86`) and injects two globals:
   - `window.YT` = `{clusters, isLocalCluster?, environment?, parameters:{interface:{version}, login, authWay}}` (`layout-config.ts:56-61`; type `YTConfig` at `src/shared/yt-types.d.ts:10-23`). **This is where `clusters-config.json` reaches the browser** — read at `src/ui/config/yt-config.ts:7`.
   - `window.__DATA__` = `ConfigData` (`layout-config.ts:62-75`; type at `yt-types.d.ts:505-518`): `{userSettingsCluster, settings:{data,meta:{useRemoteSettings,errorMessage}}, ytApiUseCORS, uiSettings, metrikaCounterId, allowPasswordAuth, allowOAuth, oauthButtonLabel, allowUserColumnPresets, odinPageEnabled, allowTabletErrorsAPI, allowPrometheusDashboards}` — read at `src/ui/config/ui-settings.ts:3-5`.

There is **no `/config` or `/clusters` endpoint** — the cluster list is
server-rendered into `window.YT`, not fetched.

Client entry point: `src/ui/entries/main.tsx:13` → `renderApp()`
(`src/ui/render-app.tsx:52-59`) → `src/ui/containers/App/App.tsx:87-119`
(`/` → clusters menu, `/:cluster/` → cluster page). A global axios
interceptor installed at `main.tsx:20-28` turns any `401` into
`handleAuthError({ytAuthCluster: response.headers['x-yt-ui-cluster-name']})`
— the node server stamps that header in `middlewares/yt-auth.ts:13`.

### 4.2 Cluster boot sequence in the browser

`ClusterPage.componentDidMount` (`src/ui/containers/ClusterPage/ClusterPage.js:106-109`)
fires `updateCluster()` (`src/ui/store/actions/cluster-params.ts:197-280`), and page
routes are mounted only after `UPDATE_CLUSTER.FINAL_SUCCESS`
(`cluster-params.ts:268-270`, gate at `ClusterPage.js:208,340,348`):

1. `GET /api/cluster-info/:cluster` (`cluster-params.ts:219`).
   Response `{token:{login, csrf_token}, version, tokenError, versionError}`
   (`src/server/controllers/cluster-info.ts` → `components/cluster-queries.ts:122-155`).
   - **Missing/failed `version` ⇒ `PRELOAD_ERROR.CONNECTION`, the app stops here** (`cluster-params.ts:235-239`).
   - **Missing `token.csrf_token` ⇒ `PRELOAD_ERROR.AUTHENTICATION`, the app stops here** (`cluster-params.ts:249-257`).
   - On success it stores `csrf_token` in the cookie `ytfront_<cluster>_xsrf_token` (`cluster-params.ts:262`, name from `src/ui/utils/index.ts:252-254`).
   Server-side this endpoint issues `GET <proxy>/auth/whoami` and `GET <proxy>/version` in parallel (`cluster-queries.ts:52-81,128-131`).
2. `initYTApiClusterParams(cluster)` (`src/ui/common/yt-api.ts:14-42`) configures the javascript-wrapper: `proxy = <window.location.host>/api/yt/<cluster>` when `ytApiUseCORS` is falsy (`src/ui/store/selectors/global/cluster.ts:40-46`), `useHeavyProxy = false`, `xsrf = true`, `xsrfCookieName = ytfront_<cluster>_xsrf_token`.
3. `checkIsDeveloper(login)` is dispatched without awaiting it — an
   `execute_batch` containing `check_permission_by_acl` against the `admins`
   group (`src/shared/utils/check-permission.ts:31-57`); failures are swallowed.
   This request can overlap the next step.
4. `GET /api/cluster-params/:cluster` (`cluster-params.ts:77`). Response is the 5-tuple built in `src/server/components/cluster-params.ts:217-223`: `{mediumList, schedulerVersion, uiConfig, uiDevConfig, masterVersion}`, each a batch item `{output?, error?}`. **A non-empty `mediumList.error` aborts cluster init** (`cluster-params.ts:101-104`); errors in `uiConfig`/`uiDevConfig` with code `500` (`NODE_DOES_NOT_EXIST`) are tolerated (`cluster-params.ts:47-65`). Server-side this is two `execute_batch` calls against the proxy with the *robot* setup (`cluster-params.ts:60-94` and `:117-180`) reading `//sys/primary_masters`, `//sys/media`, `//sys/scheduler/orchid/service/version`, `//sys/@ui_config`, `//sys/@ui_config_dev_overrides`, `//sys/primary_masters/<m>/orchid/service/version`. Results are cached (`utils/auto-updated-cache.ts`).
5. `reloadUserSettings(login)` (`src/ui/store/actions/settings/index.ts:114-135`) → `provider.create()` + `provider.getAll()`. With remote settings enabled that is `POST /api/settings/<settingsCluster>/<login>/` then `GET /api/settings/<settingsCluster>/<login>/`, where `settingsCluster = userSettingsCluster ?? cluster` (`src/ui/store/selectors/global/index.ts:151-153`). With remote settings disabled, `create()` is a no-op and `getAll()` reads `localStorage` (§5.4).
6. Once the page is mounted, `SupportedFeaturesUpdater` (`src/ui/containers/ClusterPage/SupportedFeaturesUpdater.tsx:11-16`) polls YT `get_supported_features` every 600 s (`src/ui/store/actions/global/supported-features.ts:29`).

The clusters-menu page (`/`) is separate and issues
`GET /api/clusters/versions`, `GET /api/clusters/auth-status`
(`src/ui/containers/ClustersMenu/ClustersMenuBody.tsx:37-45`,
`src/ui/store/actions/clusters-menu.ts:32,48`) and — only when
`odinPageEnabled` — `GET /api/odin/clusters/availability`
(`src/ui/pages/odin/odin-utils.ts:152-159`). None of these run on
`/:cluster/navigation`.

### 4.2.1 YT commands issued for `navigation?path=//`

All through `POST /api/yt/:cluster/api/v3/<command>`:

| Command | Purpose | Ref |
|---|---|---|
| `execute_batch` → `check_permission_by_acl` | is-developer probe (`write` on `admins`); failure is swallowed | `src/shared/utils/check-permission.ts:31-57` |
| `get_supported_features` | feature matrix, refreshed every 10 min | `src/ui/store/actions/global/supported-features.ts:29` |
| `execute_batch` → `get <path>/@type` | tablet-errors counter | `src/ui/store/actions/navigation/tabs/tablet-errors/tablet-errors-background.ts:60-70` |
| `execute_batch` → `get <path>/@` with a large `attributes` list | node meta (type, account, acl, schema, dynamic, chunk_count, …) | `src/ui/store/actions/navigation/index.ts:78-93,274+` |
| `execute_batch` → `check_permission` ×2 | `write` on the path, `use` on `//sys/accounts/<account>`; skipped when the node has no `account` | `src/ui/utils/acl/acl-api.ts:209-224`, `navigation/index.ts:144-171` |
| `list` with an attribute list | directory listing of the map node | `src/ui/store/actions/navigation/content/map-node.js:47-86` |
| `exists //sys/idm/lock` | only on error paths | `src/ui/store/actions/navigation/index.ts:192` |

A mock therefore needs at minimum: `execute_batch`, `get`, `list`,
`check_permission`, `check_permission_by_acl`, `get_supported_features`,
`exists` — plus `read_table` for the table viewer.

### 4.3 Full route table of the node server

All registered in `packages/ui/src/server/routes.ts:52-133`. `:ytAuthCluster`
is the cluster `id`.

**Essential for a mock**

| Method | Path | Handler | Notes |
|---|---|---|---|
| GET | `/`, `/:c/`, `/:c/:page`, `/:c/:page/:tab`, … | `home.ts:12` | HTML shell + `window.YT` / `window.__DATA__`. |
| GET | `/api/cluster-info/:c` | `controllers/cluster-info.ts:5` | Hard gate; see §4.2. |
| GET | `/api/cluster-params/:c` | `controllers/cluster-params.ts:7` | Hard gate on `mediumList`. |
| GET/POST/PUT | `/api/yt/:c/api/:version/:command` | `controllers/yt-api.ts:31` | The YT API tunnel — carries ~all data traffic. |

**Nice to have**

| Method | Path | Handler | Notes |
|---|---|---|---|
| GET | `/ping` | `controllers/ping.ts:3` | `{result:'pong'}`, `AuthPolicy.disabled`. |
| GET | `/api/clusters/versions` | `controllers/clusters.ts:6` | `[{id, version?}]`; calls `<proxy>/version` per cluster. |
| GET | `/api/clusters/auth-status` | `controllers/clusters.ts:16` | `{<cluster>:{authorized:boolean}}`; all-`true` without password auth. |
| GET | `/api/yt-proxy/:c/:command` | `controllers/yt-proxy-api.ts:9` | Whitelist only (`:21-24`): `hosts-all` → `<proxy>/hosts/all`, `internal-discover_versions` → `<proxy>/internal/discover_versions/v2`. Used by System→HTTP proxies and Components→Versions. |
| GET | `/api/pool-names/:c` | `controllers/scheduling-pools.ts:13` | Scheduling dialogs only. |
| GET/POST/PUT/DELETE | `/api/settings/:c/:user[/:path]` | `controllers/settings.ts` | Only reached when `userSettingsConfig` is configured. |
| POST | `/api/markdown-to-html` | `controllers/markdown-to-html.ts:5` | Annotation rendering. |
| GET/POST | `/api/table-column-preset/:c[/:hash]` | `controllers/table-column-preset.ts` | Gated by `allowUserColumnPresets`. |
| POST | `/api/remote-copy` | `controllers/remote-copy.ts:15` | |

**Feature-flagged / off by default** — never called unless the corresponding
config key is set, so a mock can ignore them entirely:

| Path | Gate |
|---|---|
| `/api/yt/:c/login`, `/api/yt/logout`, `/api/yt/:c/change-password`, `/:c/change-password/` | `allowPasswordAuth` (`ALLOW_PASSWORD_AUTH`) |
| `/oauth/login`, `/api/oauth/callback`, `/api/oauth/logout/callback` | `ytOAuthSettings` |
| `/api/odin/proxy/:action/:c?`, `/api/odin/clusters/availability` | `odinBaseUrl` → `odinPageEnabled` |
| `/api/:c/prometheus/chart-data`, `/api/:c/prometheus/discover-values` | `prometheusBaseUrl` → `allowPrometheusDashboards` |
| `/api/tablet-errors/:c/:action` | `tabletErrorsBaseUrl` → `allowTabletErrorsAPI` |
| `/api/strawberry/:engine/:c/:action` | `ui_config.chyt_controller_base_url` / `livy_controller_base_url` |
| `/api/vcs`, `/api/vcs/*` | `uiSettings.vcsSettings` |
| `/api/code-assistant/*` | AI chat config |
| `/api/access-log/:c/*`, `/api/accounts-usage/:c/*` | `check-available` probes first |

See `bootstrap-config.inventory.json` for the machine-readable version with
request/response shapes.

---

## 5. Auth, XSRF, cookies, headers

### 5.1 Node-server auth

With `allowPasswordAuth` unset:

- `allowPasswordAuth` is false and `src/server/index.ts:28-45` installs no auth
  resolver/handler (assuming OAuth is also unconfigured). The stock
  `configs/common.ts` value `appAuthPolicy: AuthPolicy.required` is retained
  because `applyAppEnvToConfig` only fills missing fields; without an auth
  handler, the policy has nothing to invoke.
- `req.yt` stays undefined, so `getUserYTApiSetup` produces empty `authHeaders` (`components/requestsSetup.ts:118-132`) — which is exactly right for `authentication: 'none'`.
- `GET /api/cluster-info/:cluster` still calls the proxy's `/auth/whoami`
  explicitly to obtain the bootstrap login and CSRF token (§4.2).

With `ALLOW_PASSWORD_AUTH=1`, `createYTAuthorizationResolver`
(`middlewares/yt-auth.ts:6-24`) turns the cookie
`<cluster>_YTCypressCookie` (`utils/index.ts:234-236`) into
`Cookie: YTCypressCookie=<secret>` for upstream calls, and
`createAuthMiddleware` (`middlewares/authorization.ts:18-42`) calls
`<proxy>/auth/whoami` on every auth-enabled request, 401-ing non-UI routes on
authentication failure.

### 5.2 XSRF / CSRF

- The browser stores `csrf_token` from `/api/cluster-info` in the cookie `ytfront_<cluster>_xsrf_token` (`src/ui/store/actions/cluster-params.ts:262`).
- The wrapper is told `xsrf: true, xsrfCookieName: ytfront_<cluster>_xsrf_token`
  (`src/ui/common/yt-api.ts:30-31`). When `authentication !== 'none'`, it also
  sets `withXSRFToken: true`, and Axios copies that cookie into the
  **`X-Csrf-Token`** request header
  (`javascript-wrapper/lib/core.js:246,264`). With `authentication: 'none'`,
  `withXSRFToken` is false and no such header is sent.
- The proxy requires `X-Csrf-Token` for every non-GET request that authenticated **via cookie** (`yt/yt/server/http_proxy/http_authenticator.cpp:214-232`); token-auth and no-auth requests are exempt. `GET /auth/whoami` mints the token (`http_authenticator.cpp:67-94`).
- Wrapper error code `XSRF_TOKEN_EXPIRED` triggers a "reload the page" toast (`src/ui/common/yt-api.ts:51-63`).

A mock with `require_authentication`-off semantics can ignore
`X-Csrf-Token` (and normally receives none in `authentication: 'none'` mode).
It still has to return **some** `csrf_token` from `/auth/whoami`, otherwise the
UI hard-fails with `PRELOAD_ERROR.AUTHENTICATION`.

### 5.3 Headers on the wire

Browser → node server → proxy (`controllers/yt-api.ts:107-125`): the node
server forwards all incoming headers except `host`, `cookie` and
`X-Custom-Request-Id`, and adds `X-YT-Correlation-Id: <req.id>`,
`X-YT-Suppress-Redirect: 1`, `accept-encoding: gzip`, plus `authHeaders`.

Wrapper-generated headers (`javascript-wrapper/lib/core.js:154-187`):
`Accept: application/json`; `X-YT-Parameters-0…N` (base64 chunks of the JSON
parameters, ≤64 KiB each, max 2) together with
`X-YT-Header-Format: <encode_utf8=%false>json` when `useEncodedParameters` is
on (`core.js:48-81`) — hence `--max-http-header-size=204800`;
`Content-Type: application/json` for POST/PUT; `X-YT-Suppress-Redirect: 1`
when heavy-proxy resolution is disabled; `X-Csrf-Token` via Axios when
`authentication !== 'none'` (`core.js:246,264`); `Authorization: OAuth <token>` only when
`authentication.type === 'oauth'` (`core.js:163-165`).

`X-Custom-Request-Id` (`src/shared/constants/index.ts:1`) is added per call by
the RUM wrapper (`src/ui/rum/rum-wrap-api.ts:296-299`) and **consumed and
stripped** by the node server (`controllers/yt-api.ts:64,107`) — it never
reaches the proxy.

Response header `x-yt-ui-cluster-name` (`src/shared/constants/index.ts:7`) is
set by the node server on cluster-scoped routes and is what the browser's 401
handler keys off.

Commands with `useBodyForParameters: true` (`get`, `list`, `exists`,
`read_table`, `execute_batch`, … — `javascript-wrapper/lib/commands/v3.js:35-55,94-106,334-339`)
send their parameters as a **JSON request body** instead, so a mock must
merge query string + `X-YT-Parameters*` + body (as `mock-backend/server.js:75-98` does).

Proxy → client (verified in `yt/yt/server/http_proxy/context.cpp`):
`X-YT-Request-Id`, `X-YT-Trace-Id`, `X-YT-Proxy`, `Cache-Control: no-store`,
and on error either headers or trailers `X-YT-Error` (JSON), `X-YT-Response-Code`,
`X-YT-Response-Message` (`core/http/helpers.cpp:43-95`; trailer declaration at
`context.cpp:832-848`). Trailers are skipped when the client sends
`X-YT-Omit-Trailers` — a mock can simply always use headers.

### 5.4 User settings — how to stub

`isRemoteSettingsConfigured()` is just `Boolean(config.userSettingsConfig)`
(`src/server/components/settings.ts:27-30`). When it is false:

- `home.ts:46-56` sets `settings.meta.useRemoteSettings = false` and ships only the server defaults;
- `src/ui/store/reducers/settings.ts:37-42` picks `settings-local-provider` and merges `localStorage` on top;
- `settings-local-provider` (`src/ui/common/utils/settings-local-provider.ts:4,8,35`) stores everything under `interface-settings/<login>/<key>` in `localStorage` (via `src/ui/common/hammer/storage.js`), `create()` is a no-op;
- **`/api/settings/*` is never called.**

Two caveats: the provider is chosen **once at module load**
(`src/ui/store/reducers/settings.ts:22-43`), so a mid-session remote failure
does not fall back to localStorage — only a reload does. And when
`userSettingsConfig` *is* set but reading the Cypress document fails, the
server itself downgrades to `useRemoteSettings: false` and ships an
`errorMessage` (`src/server/controllers/home.ts:65-79`).

So: *do not set `userSettingsConfig`* and settings persistence is stubbed for
free. If you do want to exercise the endpoints, they map 1:1 onto Cypress
`create`/`get`/`set`/`remove` of a document at
`<mapNodePath>/<username>[/<path>]` (`components/settings.ts:38-135`), and a
mock only has to make those four commands work.

### 5.5 CORS / websockets

- With `ytApiUseCORS` unset (default), ordinary wrapper API calls are
  same-origin via `/api/yt/…`; the direct download/upload paths below are the
  exceptions.
- Exception 1 — **download-ish reads**, gated by `uiSettings.directDownload` (`src/ui/utils/navigation/index.ts:148-166`): `read_table` (table download), `read_file`, `read_query_result`, `get_job_stderr`, `get_job_fail_context`, `get_job_input` go to `//<externalProxy ?? proxy>/api/v3/<cmd>` with `withCredentials: true` (`.../DownloadManager/DownloadManager.tsx:314`, `src/ui/store/selectors/navigation/content/file.js:11`, `.../Jobs/job-selector.ts:154-168`). Set `directDownload: false` to route these through the node server instead.
- Exception 2 — **uploads always bypass the node server**: `write_file` (`src/ui/containers/UploadFileManager/uploadFile.ts:18,36`) and `write_table` (`.../UploadManager/UploadManager.tsx:408,426-429`) override the wrapper `proxy` with `externalProxy ?? proxy` regardless of `directDownload`, and set `X-Csrf-Token` manually (`UploadManager.tsx:486-490`). Only relevant if your mock is writable.
- The existing mock already answers CORS preflights (`mock-backend/server.js:43-64`).
- No application websockets. The only websocket is the dev-server HMR socket at `/build/sockjs-node`. The only SSE is `text/event-stream` on the AI-chat endpoint (`src/server/controllers/ai-chat.ts:125`), which is off by default.

---

## 6. Heavy proxies — making everything hit one host

Three independent layers, all of which must point at the mock:

1. **Browser wrapper** — disabled outright: `yt.setup.setGlobalOption('useHeavyProxy', false)` (`src/ui/common/yt-api.ts:28`); the wrapper default is `true` (`lib/utils/setup.js:108`). The generic path (`javascript-wrapper/lib/core.js:331-359`) would otherwise `GET <proto>://<proxy>/hosts` (**no `role` query param in this version**), take `proxies[0]`, store it as the *global* `heavyProxy` option and build the URL from it (`core.js:197-211`) — with **no caching**, one `/hosts` call per heavy request (see the `XXX New proxy is requested every time` comment at `core.js:339-341`). Heavy commands are `read_file`, `write_file`, `read_table`, `write_table`, `select_rows`, `insert_rows` (`lib/commands/v3.js:66,84,99,119,132,177`), surfaced to the node server through `yt.getSupportedCommands()` (`lib/index.js:3-13`). Net effect: `grep '/hosts' src/ui` finds only the `/api/yt-proxy/:c/hosts-all` calls.
2. **Node server tunnel** — `controllers/yt-api.ts:92-101`:
   ```ts
   if (commandInfo?.heavy && !isLocalCluster && !setup.disableHeavyProxies) {
       const res = await axios.request({method: 'GET', url: `${proto}://${proxy}/hosts`, ...});
       requestProxy = res.data[0];
   }
   ```
   → `GET http://localhost:8000/hosts` must return a **JSON array of strings** whose first element is a host the node server can reach. `["localhost:8000"]` works. Alternatively set `disableHeavyProxies: true` in the cluster entry, or run the cluster in local mode (`isLocalCluster`).
   `scripts/dev.localmode-env.sh:25` sanity-checks exactly this shape: `curl http://$PROXY/hosts | head -n 1 | grep '\["'`. Also uncached — one `/hosts` round-trip per heavy request. Note the node server's *own* wrapper setup hard-sets `useHeavyProxy: false` (`components/requestsSetup.ts:60`), so this handler is the **only** place heavy-proxy selection happens.
3. **Direct browser URLs** — `makeDirectDownloadPath` (`src/ui/utils/navigation/index.ts:148-166`) uses `externalProxy ?? proxy` when `uiSettings.directDownload` is true. Leave `externalProxy` unset so it falls back to `proxy`.

Real-proxy semantics for reference (`yt/yt/server/http_proxy/coordinator.cpp:543-622`):

- `GET /hosts?role=<role>` — `role` defaults to `coordinator/default_role_filter`, itself defaulting to `"data"` (`config.cpp:65-66`). Returns a JSON array of hostnames (ports included only when `coordinator/show_ports` is true, `coordinator.cpp:593-601`), sorted by a fitness function and with the best half shuffled (`coordinator.cpp:218-277`). `Accept: text/plain` (exact string) switches to a newline-separated list (`coordinator.cpp:569-571,611-620`). Always `200`.
- `GET /hosts/all` — every proxy including dead/banned, as objects: `{host, name, role, banned, ban_message, dead, liveness:{updated_at, load_average, network_coef, user_cpu, system_cpu, cpu_wait, concurrent_requests}}` (`coordinator.cpp:575-591`). This is what the UI's System page consumes via `/api/yt-proxy/:c/hosts-all` and maps to `{name, state, role, banned}` (`src/ui/store/actions/system/proxies.ts:20-30`).
- A *real* proxy that cannot serve heavy requests answers heavy commands with **307** to a data proxy unless the caller sends `X-YT-Suppress-Redirect` (`context.cpp:290-308`) — the node server always sends it (`controllers/yt-api.ts:119`), and so does the wrapper when `useHeavyProxy` is off (`core.js:177-179`). A mock therefore never needs to redirect.

---

## 7. Cluster proxy endpoint contract (required and optional)

| Endpoint | Called by | Required shape |
|---|---|---|
| `GET /version` | node server, per cluster-info and per `/api/clusters/versions` (`components/cluster-queries.ts:72-81`) | **plain text** body. `/api/clusters/versions` extracts `/(\d+)\.(\d+)\.(\d+)/` (`cluster-queries.ts:83-86`); cluster-info returns the raw string and bootstrap only checks that it is truthy. Real proxy: raw `GetVersion()` string, no JSON quoting (`bootstrap.cpp:480-483`). |
| `GET /auth/whoami` | node server (`cluster-queries.ts:52-69`) and the auth middleware | `{"login": "...", "realm": "...", "real_login": "...", "csrf_token": "..."}` (`http_authenticator.cpp:67-94`). `login` and `csrf_token` are the two fields the UI needs. **Missing → the app never renders.** |
| `GET /hosts` | node server for heavy commands (`controllers/yt-api.ts:96`) | JSON array of strings, `["localhost:8000"]`. |
| `GET /hosts/all` | System page via `/api/yt-proxy/:c/hosts-all` | array of `{host,name,role,banned,ban_message,dead,liveness}`. |
| `GET /internal/discover_versions/v2` | Components→Versions via `/api/yt-proxy/:c/internal-discover_versions` | `{summary: {<version>: {<type>: {total,banned,offline}}}, details: [...]}` (`coordinator.cpp:697-733`). |
| `POST /api/v3/execute_batch` | node server for `/api/cluster-params` (`components/cluster-params.ts:61,118`) | `[{output?, error?}, …]`, one entry per sub-request, in order. |
| `POST /api/v3/<command>` or `/api/v4/<command>` | everything else, tunnelled through `/api/yt/:c/api/:v/:cmd` | Standard YT command semantics. |
| `GET /ping` | not used by the UI | Real proxy: `200`/`503`, empty body (`coordinator.cpp:656-670`). |
| `GET /api`, `GET /api/v4` | not used by the UI | Discovery: `["v3","v4"]` (`context.cpp:140-152`) and the command-descriptor list `[{name,input_type,output_type,is_volatile,is_heavy}]` (`context.cpp:162-180`, `client/driver/driver.cpp:73-83`). |
| `POST /api/v4/discover_proxies` | not used by the UI | `{"proxies": ["host:port", …]}` (`client/driver/etc_commands.cpp:491-518`). Params `kind`/`type`, `role`, `address_type`, `network_name`. |
| `POST /login` | only with `ALLOW_PASSWORD_AUTH` (`controllers/login.ts:33`) | Basic-auth in, `Set-Cookie: YTCypressCookie=…` out. |

Command version note: the node server validates `:command` against the
wrapper's command table before proxying and rejects unknown ones with 400
(`controllers/yt-api.ts:51-59`).

---

## 8. Current `mock-backend/server.js` coverage

Checked against `/shared/ytsaurus4/iceberg-viewer/mock-backend/server.js`:

- The bootstrap path is implemented: `/version`, `/auth/whoami`, anonymous
  access for `authentication: "none"`, and `execute_batch` with the
  `//sys/primary_masters`, `//sys/media`, scheduler-version, and UI-config reads
  used by `/api/cluster-params`.
- The Navigation and static-table path is implemented for v3 and v4:
  `get`, `list`, `exists`, `read_table`, `execute_batch`, `check_permission`,
  `check_permission_by_acl`, `get_supported_features`,
  `get_table_columnar_statistics`, and `whoami`.
- Password login accepts HTTP Basic credentials at `/login` and returns a
  `YTCypressCookie`. The mock also enforces `X-Csrf-Token` for non-GET requests
  authenticated by one of its issued cookies.
- `/hosts` returns `[HOST]`, `/hosts/all` returns an empty object-list-compatible
  array, and `/version` returns `mock-proxy-1.0.0`.

Remaining limitations are outside the documented Navigation/table-viewer
surface:

- `/internal/discover_versions/v2` is not implemented, so
  Components → Versions cannot load.
- `/api/v4` command discovery is not implemented (the UI does not call it).
- Commands beyond the list above return a YT-shaped `404`; pages that need
  those commands are not covered.
- An unknown or expired `YTCypressCookie` falls back to the anonymous `iceberg`
  user, so the mock does not exercise session-expiry 401/login-dialog behavior.
