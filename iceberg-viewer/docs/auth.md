# YTsaurus UI ↔ backend authentication, at wire level

Scope: everything the browser, the UI's own Node/Express server, and the cluster
HTTP proxy exchange in order to consider a user "logged in", plus the minimum a
mock backend has to implement.

Source trees referenced below:

| alias | path |
|---|---|
| `ui/` | `/shared/ytsaurus4/iceberg-viewer/ytsaurus-ui/packages/ui` |
| `jsw/` | `/shared/ytsaurus4/iceberg-viewer/ytsaurus-ui/packages/javascript-wrapper` |
| `yt/` | `/shared/ytsaurus4/yt/yt` |

Three tiers exist and must not be confused:

```
browser  ──(1)──►  UI Node server (expresskit)  ──(2)──►  cluster HTTP proxy (C++)
         ◄─────                                 ◄─────
```

* (1) is same-origin; cookies are browser cookies on the UI host.
* (2) is server-to-server; the Node server *manufactures* a `Cookie:` (or
  `Authorization:`) header from what it read out of the browser's cookies.

---

## 0. The three authentication modes and how to turn auth off

### 0.1 `allowPasswordAuth` — the master switch of the UI server

`ui/src/server/utils/configs/apply-app-env-to-config.ts:29-41`:

```ts
const {ALLOW_PASSWORD_AUTH, WITH_AUTH, YT_AUTH_CLUSTER_ID, YT_AUTH_ALLOW_INSECURE} = process.env;
const allowPasswordAuth = Boolean(ALLOW_PASSWORD_AUTH || WITH_AUTH || YT_AUTH_CLUSTER_ID);
const tmp = {
    allowPasswordAuth,
    ytAuthAllowInsecure: Boolean(YT_AUTH_ALLOW_INSECURE),
    ...{appAuthPolicy: allowPasswordAuth ? AuthPolicy.required : AuthPolicy.disabled},
};
```

So: **if none of `ALLOW_PASSWORD_AUTH`, `WITH_AUTH`, `YT_AUTH_CLUSTER_ID` is set
in the UI server's environment, `appAuthPolicy` becomes `AuthPolicy.disabled`.**

`ui/src/server/index.ts:26-45` then wires middleware only when auth is on:

```ts
const {allowPasswordAuth, ytOAuthSettings} = nodekit.config;
const authMiddlewares = [];
if (ytOAuthSettings)   authMiddlewares.push(authorizationResolver(createOAuthAuthorizationResolver()));
if (allowPasswordAuth) authMiddlewares.push(authorizationResolver(createYTAuthorizationResolver()));
if (authMiddlewares.length) {
    nodekit.config.appBeforeAuthMiddleware = [...(…||[]), ...authMiddlewares];
    nodekit.config.appAuthHandler = createAuthMiddleware();
}
```

And expresskit skips the auth handler entirely when the policy is `disabled`
(`ui/node_modules/@gravity-ui/expresskit/dist/router.js:140-142`):

```js
const authHandler = authPolicy === AuthPolicy.disabled ? undefined
                                                       : route.authHandler || ctx.config.appAuthHandler;
```

Consequences when `allowPasswordAuth` is falsy:

* No `resolveYTAuthorization` middleware ⇒ `req.yt` is **undefined**.
* No `authMiddleware` ⇒ nothing ever calls `/auth/whoami`, nothing ever 401s.
* `getLayoutConfig` ships `allowPasswordAuth: false` to the browser
  (`ui/src/server/components/layout-config.ts:68`), and the client's
  `selectGlobalShowLoginDialog` (`ui/src/ui/store/selectors/global/index.ts:117-129`)
  returns falsy, so **the login form is never rendered**.
* `GET /api/clusters/auth-status` reports every cluster as
  `{"authorized": true}` (`ui/src/server/controllers/clusters.ts:31-39`).

### 0.2 `authentication` in `clusters-config.json` — the per-cluster switch

Type: `ui/src/shared/yt-types.d.ts:125`

```ts
authentication?: 'none' | 'basic' | 'domain';
```

Example file `ui/clusters-config.json.example`:

```json
{"clusters": [{"id": "my-cluster", "name": "My cluster",
               "proxy": "my_cluster_proxy.my-domain.com",
               "secure": true, "authentication": "domain", ...}]}
```

Where it is consumed:

* `ui/src/server/components/requestsSetup.ts:57` — defaults to `'none'` when absent.
* `ui/src/server/components/requestsSetup.ts:118-132` — `getUserYTApiSetup()`:

  ```ts
  const authHeaders = authentication && authentication !== 'none'
      ? req.yt.ytApiAuthHeaders || {}
      : {};
  ```

  With `authentication: 'none'` the Node server sends **no** credential header to
  the proxy at all, and — importantly — it never touches `req.yt`, so it does not
  crash when the auth middleware was never installed.

* `ui/src/server/components/requestsSetup.ts:84-111` — `getRobotYTApiSetup()`
  (used for cluster-params, remote user settings): with `'none'` it sends nothing,
  otherwise `Authorization: OAuth <token>` where the token comes from
  `ytInterfaceSecret` JSON (`{"oauthToken": "..."}`) or `process.env.YT_TOKEN`
  (`requestsSetup.ts:8-28`).

* Browser side: `ui/src/ui/common/yt-api.ts:39`
  `yt.setup.setGlobalOption('authentication', {type: config.authentication || 'none'})`.
  In `jsw/lib/core.js:246` this drives
  `withCredentials = Boolean(authentication.type) && authentication.type !== 'none'`,
  i.e. with `'none'` the browser does not even send cookies on the XHRs.

* Local mode (`PROXY` env var, no clusters-config):
  `ui/src/server/config.localcluster.ts:20`
  `authentication: allowPasswordAuth ? 'basic' : 'none'`.

**`'basic'` vs `'domain'` vs `'none'`**: the UI code only ever tests
`!== 'none'`. `'basic'` and `'domain'` are behaviourally identical in the OSS UI;
both simply mean "forward the user's credentials to the proxy". The only place
the literal `'basic'` appears is the local-cluster config above.

### 0.3 `ytAuthAllowInsecure` — allow the session cookie over plain HTTP

`ui/src/server/controllers/login.ts:89-118`. When `ytAuthAllowInsecure` is true
**and** the request `Origin` starts with `http://`, the ` Secure;` attribute is
stripped from every `Set-Cookie` line that mentions `YTCypressCookie` before it
is relayed to the browser. Set automatically in local mode
(`ui/src/server/configs/local.ts:6`) or by env `YT_AUTH_ALLOW_INSECURE`.

Without this, a proxy that returns `Secure` cookies makes login silently fail on
an `http://` UI.

### 0.4 OAuth

`ytOAuthSettings` in the Node config enables the SSO flow
(`ui/docs/configuration.md:156-172`, `ui/src/server/components/oauth.ts:12-22`).
It is orthogonal to `allowPasswordAuth` and is described in §3.4.

---

## 1. Password login: exact wire trace

### 1.1 Browser → UI server

`ui/src/ui/containers/Login/LoginFormPage/LoginFormPage.tsx:176-189`:

```ts
function authorize({username, password, ytAuthCluster}) {
    return axios.post(`/api/yt/${ytAuthCluster}/login`, {username, password});
}
```

Wire:

```http
POST /api/yt/mock/login HTTP/1.1
Host: ui.example.com
Content-Type: application/json
Origin: http://ui.example.com

{"username":"admin","password":"secret"}
```

Note: the body parsers are disabled for every `/api/yt/` URL
(`ui/src/server/configs/common.ts:13-41`), so `req.body` is a raw `Buffer`, which
is why the handler does `JSON.parse(req.body)`
(`ui/src/server/controllers/login.ts:27`).

### 1.2 UI server → cluster proxy

`ui/src/server/controllers/login.ts:23-78`:

```ts
const {proxyBaseUrl} = getYTApiClusterSetup(ytAuthCluster);   // http(s)://<cluster.proxy>
const requestUrl = `${proxyBaseUrl}/login`;                   // ← the proxy endpoint
const basicAuth = Buffer.from(`${username}:${password}`).toString('base64');
await axios.request({
    url: requestUrl,
    method: req.method,                                       // POST
    headers: {...req.ctx.getMetadata(), Authorization: `Basic ${basicAuth}`},
    timeout: 10000,
    responseType: 'stream',
});
```

Wire:

```http
POST /login HTTP/1.1
Host: mock-proxy:8000
Authorization: Basic YWRtaW46c2VjcmV0
x-request-id: 3f0a…            ; from ctx.getMetadata()
```

There is **no** request body and **no** `_login_password_` path in the OSS UI:
the UI hits the proxy's plain `/login` route with HTTP Basic credentials. The
password is sent in clear (base64) to the proxy, which hashes it itself. The
same flow is exercised by `ui/tests/init-cluster-e2e/utils/add.user.sh:17-21`:

```bash
export YT_CYPRESS_COOKIE=$(
  curl -v http://localhost:8000/login \
    -H "Authorization: Basic $(echo -n admin:${YT_TOKEN} | base64)" 2>&1 \
    | grep YTCypressCookie | awk -F ":|;" '{print $2}')
```

### 1.3 Cluster proxy → UI server (Set-Cookie)

The proxy answers `200` and sets the Cypress session cookie. Cookie name
constant on the UI side: `ui/src/shared/constants/index.ts:2`

```ts
export const YT_CYPRESS_COOKIE_NAME = 'YTCypressCookie';
```

### 1.4 UI server → browser (cookie duplication)

`ui/src/server/controllers/login.ts:45-78` streams the proxy response back
verbatim, but rewrites `set-cookie`: for every line that starts with
`YTCypressCookie` it *appends a copy* renamed to `<cluster>_YTCypressCookie`
(`makeAuthClusterCookieName`, `ui/src/server/utils/index.ts:234-236`):

```ts
export const makeAuthClusterCookieName = (ytAuthCluster: string) =>
    `${ytAuthCluster}_${YT_CYPRESS_COOKIE_NAME}`;
```

then `removeSecureFlagIfOriginInsecure()` (§0.3) runs.

Wire (two clusters would give two extra lines; here cluster id is `mock`):

```http
HTTP/1.1 200 OK
Content-Type: application/json
Set-Cookie: YTCypressCookie=<opaque>; Path=/; HttpOnly; Secure; SameSite=Lax; Expires=...
Set-Cookie: mock_YTCypressCookie=<opaque>; Path=/; HttpOnly; Secure; SameSite=Lax; Expires=...

{}
```

`pipeResponse` (`ui/src/server/utils/index.ts:132-154`) copies every header
except `content-length`, `vary` and **`www-authenticate`** — the last exclusion
is deliberate so that a `401` from the proxy does not make the browser pop a
native Basic-auth dialog.

Error path: `login.ts:79-86` rewrites a proxy `401` into `400` before
`sendAndLogError`, "to avoid redirecting to login page when checking user
password". Because of `pipeAxiosErrorOrFalse`
(`ui/src/server/utils/index.ts:189-201`) the proxy's own YT-error JSON body is
piped through unchanged with the proxy's status code whenever the error response
is a stream — so in practice the browser sees the proxy's `401` body/status for
a wrong password, and `LoginFormPage.tsx:82-90` renders `error.response.data.message`.

### 1.5 After a successful login

`LoginFormPage.tsx:77-81` → `onSuccessLogin(username)`
(`ui/src/ui/store/actions/global/index.ts:335-346`) which only mutates the redux
store (`showLoginDialog: false, login, ytAuthCluster: undefined`) and reloads
user settings. **No page reload, no second network call** is required for the
login form to disappear.

---

## 2. Credentials on every subsequent request

### 2.1 What the browser holds

| cookie | set by | read by | HttpOnly |
|---|---|---|---|
| `YTCypressCookie` | cluster proxy (relayed) | browser→proxy on direct CORS calls | yes (proxy's choice) |
| `<cluster>_YTCypressCookie` | UI server, `login.ts:57-63` | UI Node server only | inherits proxy's attrs |
| `ytfront_<cluster>_xsrf_token` | **client JS**, `cluster-params.ts:262` | client JS (axios xsrf) | no |
| `yt_oauth_access_token` | UI server, `oauth.ts:67-72` | UI Node server | yes, `Secure` |
| `yt_oauth_refresh_token` | UI server, `oauth.ts:73-79` | UI Node server | yes, `Secure` |

Constants: `ui/src/shared/constants/index.ts:2,5,6`;
`getXsrfCookieName` = `` `ytfront_${cluster}_xsrf_token` `` at
`ui/src/ui/utils/index.ts:252-254`.

### 2.2 How the Node server turns a browser cookie into a proxy credential

`ui/src/server/middlewares/yt-auth.ts:6-24` (runs as `appBeforeAuthMiddleware`):

```ts
const secret: string = req.cookies[makeAuthClusterCookieName(ytAuthCluster)];
if (ytAuthCluster) res.setHeader(YT_UI_CLUSTER_HEADER_NAME, ytAuthCluster);   // 'x-yt-ui-cluster-name'
req.yt = {ytApiAuthHeaders: {Cookie: `${YT_CYPRESS_COOKIE_NAME}=${secret};`}};
```

i.e. the per-cluster cookie is renamed **back** to `YTCypressCookie` for the
upstream request. The OAuth resolver does the analogous thing
(`ui/src/server/middlewares/oauth.ts:19-24`):

```ts
const token = await getOAuthAccessToken(req, res);
req.yt = {ytApiAuthHeaders: {Cookie: `access_token=${token}`}};
```

Both are wrapped in `authorizationResolver`
(`ui/src/server/utils/authorization.ts:23-32`) so OAuth wins if it already
produced credentials.

⚠ Gotcha worth knowing when mocking: `isAuthorized()`
(`ui/src/server/utils/authorization.ts:6-11`) is

```ts
return Boolean(Object.keys(req.yt.ytApiAuthHeaders ?? {}));
```

`Boolean([])` is `true`, so this is effectively "`req.yt` exists". The real
authorization decision is therefore **entirely** delegated to the `/auth/whoami`
call below — even a request with `Cookie: YTCypressCookie=undefined` passes
`isAuthorized`.

### 2.3 The auth handler: `/auth/whoami` is the gate

`ui/src/server/middlewares/authorization.ts:18-42`:

```ts
if (!isAuthorized(req)) throw new AuthError();
const cfg = getUserYTApiSetup(ytAuthCluster, req);
const {login} = await getXSRFToken(req, cfg);
req.yt.login = login;
…
if (!req.routeInfo.ui && isAuthFailed) { sendError(res, error, 401); return; }
next();
```

`getXSRFToken` (`ui/src/server/components/cluster-queries.ts:25-70`):

```ts
axios.request<{login: string; csrf_token: string}>({
    url: proxyBaseUrl + '/auth/whoami',
    method: 'GET',
    headers: {...authHeaders, 'X-YT-Correlation-Id': `${req.id}.getXSRFToken`},
    timeout: 15000,
});
```

Wire (server → proxy):

```http
GET /auth/whoami HTTP/1.1
Host: mock-proxy:8000
Cookie: YTCypressCookie=<opaque>;
X-YT-Correlation-Id: 1a2b3c.getXSRFToken
```

Expected `200` body — **exactly these two fields are consumed**:

```json
{"login": "admin", "csrf_token": "b3f1…:1737800000"}
```

Anything non-200 ⇒ axios throws ⇒ `isAuthError` (`authorization.ts:14-16`
matches `e.response?.status === 401`) ⇒ for non-`ui` routes the Node server
answers the browser:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
x-yt-ui-cluster-name: mock

{"message":"…"}          ; sendError → prepareErrorToSend, ui/src/server/utils/index.ts:47-70
```

For routes flagged `ui: true` (all HTML pages, `/api/yt/:cluster/login`,
`/api/yt/logout`, the oauth routes) the middleware **swallows** the failure and
calls `next()`, so the page still renders — just with `login === undefined`,
which is what makes the login form appear.

### 2.4 The browser's reaction to 401

`ui/src/ui/entries/main.tsx:10-28` installs a global axios interceptor and a
`yt.subscribe('error')` hook:

```ts
const isAuthError = axios.isAxiosError(e) && e.response?.status === 401;
if (isAuthError) handleAuthError({ytAuthCluster: e.response?.headers[YT_UI_CLUSTER_HEADER_NAME]});
```

`handleAuthError` (`ui/src/ui/store/actions/global/index.ts:314-333`) needs the
`x-yt-ui-cluster-name` response header; without it it only shows a toaster
("Failed to show a login form. ytAuthCluster is not defined") and never opens the
form. The header is set by the yt-auth / oauth middlewares
(`yt-auth.ts:13`, `oauth.ts:16`).

### 2.5 CSRF token

Flow, end to end:

1. Node server obtains `csrf_token` from `/auth/whoami` — see §2.3 — inside
   `getClusterInfo` (`ui/src/server/components/cluster-queries.ts:122-155`).
2. It is returned to the browser by `GET /api/cluster-info/:cluster`
   (`ui/src/server/controllers/cluster-info.ts:5-16`) as
   `{token: {login, csrf_token}, version, tokenError, versionError}`.
3. The client stores it in a **JS-readable cookie**
   (`ui/src/ui/store/actions/cluster-params.ts:258-262`):

   ```ts
   const {login, csrf_token} = token;
   YT.parameters.login = login;
   dispatch({type: GLOBAL_PARTIAL, data: {login}});
   Cookies.set(getXsrfCookieName(cluster), csrf_token);   // ytfront_<cluster>_xsrf_token
   ```

   If `csrf_token` is missing or `tokenError` is set, the cookie is removed and
   the app renders the `PRELOAD_ERROR.AUTHENTICATION` screen
   (`cluster-params.ts:249-257`, `ui/src/ui/containers/PreloadError/PreloadError.tsx:57`).
4. The javascript-wrapper is configured with
   `xsrf: true`, `xsrfCookieName: ytfront_<cluster>_xsrf_token`
   (`ui/src/ui/common/yt-api.ts:30-31`) and axios turns that into the request
   header `X-Csrf-Token` (`jsw/lib/core.js:264`):

   ```js
   xsrfEnabled ? {xsrfCookieName, xsrfHeaderName: 'X-Csrf-Token'} : undefined
   ```
5. The Node proxy handler forwards all browser headers except `host`,
   `cookie` and `X-Custom-Request-Id`, so `x-csrf-token` reaches the cluster
   proxy unchanged (`ui/src/server/controllers/yt-api.ts:107-121`).

Header spelling on the wire is case-insensitively `X-Csrf-Token`; the shell
example in `ui/tests/init-cluster-e2e/utils/add.user.sh:23,29` uses the same:

```bash
export X_CSRF_TOKEN=$(curl -H "Cookie: $YT_CYPRESS_COOKIE" \
    http://localhost:8000/auth/whoami | json_pp | grep csrf_token | awk -F '"' '{print $4}')
curl -X POST -H "X-Csrf-Token: $X_CSRF_TOKEN" -H "Cookie: $YT_CYPRESS_COOKIE" \
    http://localhost:8000/api/v4/set_user_password --data-raw '{...}'
```

Note the *other* CSRF mechanism, expresskit's own
(`ui/node_modules/@gravity-ui/expresskit/dist/csrf.js`), is **not** active: it is
only installed when `ctx.config.appCsrfSecret` is set (`router.js:145-147`) and
no YTsaurus config sets it.

### 2.6 Proxying API calls to the cluster

`ui/src/server/controllers/yt-api.ts:31-141`, route
`GET|POST|PUT /api/yt/:ytAuthCluster/api/:version/:command`.

* URL built at `yt-api.ts:104`:
  `${proto}://${requestProxy}/api/${version}/${command}${search}` — the query
  string is re-serialized from `req.query` with `qs`.
* `commandInfo.heavy` commands first do `GET ${proto}://${proxy}/hosts` and use
  `res.data[0]` as the target host (`yt-api.ts:92-101`) unless the cluster is
  local or `disableHeavyProxies` is set.
* Headers sent upstream (`yt-api.ts:113-121`):
  `x-request-id` (ctx metadata) + all browser headers except `host`/`cookie`/
  `X-Custom-Request-Id`, plus `accept-encoding: gzip`,
  `X-YT-Correlation-Id: <req.id>`, `X-YT-Suppress-Redirect: 1`, and finally
  `...authHeaders` (`Cookie: YTCypressCookie=…` or `Cookie: access_token=…`,
  or nothing when `authentication === 'none'`).
* The command whitelist is the javascript-wrapper's own command table
  (`yt.getSupportedCommands()`, `yt-api.ts:29,51-59`) plus
  `get_job_stderr | get_job_input | get_job_fail_context` (`yt-api.ts:27`).
  Unknown version/command ⇒ `400`.
* Response is streamed back byte-for-byte with all proxy headers (minus
  `content-length`, `vary`, `www-authenticate`).

The browser addresses this route through `getClusterProxy`
(`ui/src/ui/store/selectors/global/cluster.ts:40-46`):

```ts
const allowYtTvmApi = !getConfigData().ytApiUseCORS;
if (allowYtTvmApi) return `${window.location.host}/api/yt/${clusterConfig.id}`;
return clusterConfig.proxy;
```

and `jsw/lib/core.js:197-211` appends `/api/<version>/<command>`. So with the
default (`ytApiUseCORS` falsy) every cluster call goes
`browser → /api/yt/<cluster>/api/v4/<cmd> → proxy /api/v4/<cmd>`.
With `ytApiUseCORS: true` the browser talks to `clusterConfig.proxy` directly
and must carry `YTCypressCookie` itself (`withCredentials`, `core.js:246,260`).

### 2.7 Direct-to-proxy OAuth token (server side, "robot")

`jsw/lib/core.js:161-165`:

```js
const authentication = setup.getOption(localSetup, 'authentication');
if (authentication.type === 'oauth' && authentication.token) {
    headers['Authorization'] = 'OAuth ' + authentication.token;
}
```

Used by `getRobotYTApiSetup` (`requestsSetup.ts:84-111`) for cluster-params and
remote user-settings requests. Token source: `ytInterfaceSecret` JSON file
(`{"oauthToken": "..."}`, optionally keyed per cluster) or `YT_TOKEN` env.

### 2.8 Logout

`ui/src/server/controllers/logout.ts:5-12`:

```ts
if (isOAuthAllowed(req) && isUserOAuthLogged(req)) res.redirect(getOAuthLogoutPath(req));
else if (isYtAuthEnabled(req.ctx.config)) YTAuthLogout(res);
res.redirect('/');
```

`YTAuthLogout` (`ui/src/server/components/yt-auth.ts:11-22`) writes a single
`set-cookie` header with one deletion per cluster:

```http
HTTP/1.1 302 Found
Location: /
Set-Cookie: YTCypressCookie=deleted; Path=/; Max-Age=0;
Set-Cookie: mock_YTCypressCookie=deleted; Path=/; Max-Age=0;
```

The cluster-side session (the proxy's Cypress cookie record) is **not** revoked —
logout is purely cookie deletion. The JS-set `ytfront_<cluster>_xsrf_token`
cookie is also not deleted here.

The menu entry is a plain link (`ui/src/ui/containers/AppNavigation/AppNavigationComponent.tsx:192`):
`<Menu.Item href={'/api/yt/logout'}>`.

---

## 3. Auth-related endpoints exposed by the UI Node server

All from `ui/src/server/routes.ts`. `ui: true` means "an auth failure must not
produce a 401, render the page instead" (`middlewares/authorization.ts:34-37`).

### 3.1 `POST /api/yt/:ytAuthCluster/login` — `routes.ts:61`, `ui: true`

* Handler `handleLogin`, `controllers/login.ts:23`.
* Body: raw JSON `{"username": string, "password": string}` (parsers disabled for
  `/api/yt/*`, `configs/common.ts:16-21`). Empty username/password ⇒ 500
  `{"message":"Error: Username and password must not be empty"}`.
* Upstream: `POST http(s)://<cluster.proxy>/login` with
  `Authorization: Basic base64(user:pass)`.
* Response: the proxy's status/body/headers, plus a duplicated
  `<cluster>_YTCypressCookie` `Set-Cookie`.

### 3.2 `POST /api/yt/:ytAuthCluster/change-password` — `routes.ts:77`, `ui: true`

* Handler `handleChangePassword`, `controllers/login.ts:120-168`.
* Body: `{"newPassword": string, "currentPassword": string}` (client:
  `ui/src/ui/containers/Login/ChangePasswordFormPage/ChangePasswordFormPage.tsx:159-172`).
* Server SHA-256s both (`login.ts:133-137`), fetches `login` + `csrf_token` via
  `getXSRFToken` (`login.ts:148`), sets `requestHeaders: {'X-Csrf-Token': csrf_token}`
  (`login.ts:150-152`) and calls the javascript-wrapper
  `yt.v4.setUserPassword` ⇒ upstream
  `POST /api/v4/set_user_password` with parameters
  `{user, new_password_sha256, current_password_sha256}`.
* Success: `200 {"result": <driver result>}` (`login.ts:160`).

### 3.3 `GET /api/yt/logout` — `routes.ts:62`, `ui: true`

See §2.8. `302` to `/` + cookie deletions.

### 3.4 OAuth trio

| route | line | handler | behaviour |
|---|---|---|---|
| `GET /oauth/login` | 64 | `oauthLogin`, `controllers/oauth-login.ts:9-11` | `302` to `<baseURL><authPath>?response_type=code&client_id&scope&redirect_uri=<base>/api/oauth/callback&state=state_<uuid>`; also sets a cookie named `state_<uuid>` whose value is `{"retPath": "<pathname+search of Referer>"}` (`components/oauth.ts:82-103`) |
| `GET /api/oauth/callback` | 65 | `oauthCallback`, `oauth-login.ts:18-41` | reads `?code=&state=`, POSTs `grant_type=authorization_code` to `<baseURL><tokenPath>` (`oauth.ts:146-170`), stores `yt_oauth_access_token` (`maxAge=expires_in*1000`, `httpOnly`, `secure`) and `yt_oauth_refresh_token` (`maxAge=refresh_expires_in*1000`), then `302` to the saved `retPath` (default `/`) |
| `GET /api/oauth/logout/callback` | 66 | `oauthLogout`, `oauth-login.ts:13-16` | `res.clearCookie` on both OAuth cookies, `302 /` |

Token refresh: if only the refresh cookie is present, `getOAuthAccessToken`
(`oauth.ts:47-59`) POSTs `grant_type=refresh_token` and re-sets the cookies.

The button is rendered only when `allowOAuth` is in `window.__DATA__`
(`layout-config.ts:69`, `LoginFormPage.tsx:151-164`), which requires all six of
`baseURL, authPath, tokenPath, clientId, clientSecret` (+`scope` used) to be
configured (`oauth.ts:12-22`).

### 3.5 `GET /api/cluster-info/:ytAuthCluster` — `routes.ts:56`

Not an "auth endpoint" per se but it is where the browser learns **who it is**.

```http
GET /api/cluster-info/mock HTTP/1.1
Cookie: mock_YTCypressCookie=<opaque>
```
```json
{"token": {"login": "admin", "csrf_token": "b3f1…"},
 "version": "24.1.0",
 "tokenError": null, "versionError": null}
```

`getClusterInfo` (`components/cluster-queries.ts:122-155`) runs `/auth/whoami`
and `/version` in `Promise.allSettled`; failures are wrapped as
`{message, code: <http status>, inner_errors: [<proxy body>]}`
(`cluster-queries.ts:102-120`).

### 3.6 `GET /api/clusters/auth-status` — `routes.ts:59`

`controllers/clusters.ts:16-42`. Pure cookie presence check, no upstream call:

```json
{"mock": {"authorized": true}}
```

* `allowPasswordAuth` on ⇒ `authorized = Boolean(req.cookies['<cluster>_YTCypressCookie'])`.
* `allowPasswordAuth` off ⇒ every cluster hard-coded to `{"authorized": true}`.

Consumed by the clusters menu (`ui/src/ui/containers/ClustersMenu/ClustersMenuBody.tsx:41,107`)
to draw the "not authorized" badge.

### 3.7 `GET /ping` — `routes.ts:55`, `authPolicy: AuthPolicy.disabled`

`controllers/ping.ts` ⇒ `200 {"result":"pong"}`. The only route that explicitly
opts out of the auth handler.

### 3.8 Routes that merely *consume* the credentials

These have no auth-specific payload but will 401 through `authMiddleware` if
`/auth/whoami` fails, so a mock must satisfy them too:

* `GET|POST|PUT /api/yt/:ytAuthCluster/api/:version/:command` (`routes.ts:82-84`) — §2.6.
* `GET /api/yt-proxy/:ytAuthCluster/:command` (`routes.ts:86`) — whitelist
  `hosts-all → /hosts/all`, `internal-discover_versions → /internal/discover_versions/v2`
  (`controllers/yt-proxy-api.ts:21-24`), forwards `authHeaders`.
* `GET /api/cluster-params/:ytAuthCluster` (`routes.ts:57`) — uses the **robot**
  setup, i.e. `Authorization: OAuth …` or nothing; runs a v3 `execute_batch` of
  `list //sys/media`, `get //sys/scheduler/orchid/service/version`,
  `get //sys/@ui_config`, `get //sys/@ui_config_dev_overrides`, and the primary
  master version (`components/cluster-params.ts:60-180`).
* `GET /api/clusters/versions` (`routes.ts:58`) — `GET <proxy>/version` per
  cluster, no auth headers (`cluster-queries.ts:72-100`).
* `GET|POST|PUT|DELETE /api/settings/:ytAuthCluster/:username[/:path]`
  (`routes.ts:93-97`) — robot credentials, only active when `userSettingsConfig`
  is configured (`components/settings.ts:27-30`).

### 3.9 HTML routes

`GET /`, `GET /:ytAuthCluster/...`, `GET /:ytAuthCluster/change-password/`
(`routes.ts:53,54,127-132`) all go to `homeIndexFactory()`
(`controllers/home.ts:12-92`) with `ui: true`. The rendered page embeds:

```html
<script>window.YT = Object.assign(window.YT || {}, {clusters:…, environment:…},
        {parameters: {interface:{version}, login: "admin", authWay: "passwd"}});</script>
<script type="application/json" id="__DATA__">{"allowPasswordAuth":true,"allowOAuth":false,…}</script>
```

(`components/layout-config.ts:34-84`). `login` comes from `req.yt?.login` — which
`authMiddleware` filled from `/auth/whoami` — except in local-cluster mode where
it is hard-coded to `'root'` (`home.ts:29`):

```ts
const login = ytConfig.isLocalCluster ? 'root' : req.yt?.login;
```

`authWay` is `'oauth' | 'passwd' | undefined` (`utils/authorization.ts:13-21`).

The login form is shown iff (`ui/src/ui/store/selectors/global/index.ts:117-129`,
`ui/src/ui/containers/App/App.tsx:64,72-73`):

```
allowPasswordAuth && (!login || showLoginDialog)              // when authWay is undefined
allowPasswordAuth && ytAuthCluster && (!login || showLoginDialog)   // when authWay is set
```

---

## 4. What the cluster HTTP proxy actually implements

### 4.1 Route table

`TBootstrap::RegisterRoutes`, `yt/server/http_proxy/bootstrap.cpp:623-664`:

```cpp
server->AddHandler("/auth/whoami", AllowCors(HttpAuthenticator_));      // :625
server->AddHandler("/api/",        AllowCors(Api_));                    // :626
server->AddHandler("/hosts/",      AllowCors(HostsHandler_));           // :627
server->AddHandler("/cluster_connection/", AllowCors(...));             // :628
server->AddHandler("/ping/",       AllowCors(PingHandler_));            // :629
if (CypressCookieLoginHandler_) {
    server->AddHandler("/login/",  CypressCookieLoginHandler_);         // :630-632  (NOT CORS-wrapped)
}
server->AddHandler("/internal/discover_versions/v2", AllowCors(...));   // :634
server->AddHandler("/version", AllowCors(MakeStrong(this)));            // :638
server->AddHandler("/service", AllowCors(MakeStrong(this)));            // :639
server->AddHandler("/query"); server->AddHandler("/chyt");              // :642-643  CHYT
```

Path matching is Go-`ServeMux`-like (`yt/core/http/server.cpp:625-689`): a
pattern ending in `/` also matches the bare path, so `/login/` serves `/login`
too. Unmatched ⇒ `404` (`server.cpp:338-344`).

**There is no `/logout` endpoint on the proxy** — no handler anywhere in the
tree. Logging out is purely a UI-side cookie deletion (§2.8).

`/login` exists **only** if `auth/cypress_cookie_manager` is configured
(`bootstrap.cpp:282-297`).

`/api/` is one handler for both versions (`TContext::TryParseCommandName`,
`yt/server/http_proxy/context.cpp:138-197`):
`/api` → `200 ["v3","v4"]`; `/api/v3` / `/api/v4` prefix selects the version;
`/api/v4/` with empty command dumps the command descriptors; command names must
match `[A-Za-z_]+` else `404 Malformed command name`.

### 4.2 `GET /auth/whoami`

`THttpAuthenticator::HandleRequest`, `yt/server/http_proxy/http_authenticator.cpp:67-94`:

```cpp
auto result = Authenticate(req, true);            // :69  ← CSRF check DISABLED for whoami
if (result.IsOK()) {
    rsp->SetStatus(EStatusCode::OK);              // :71
    ProtectCsrfToken(rsp);                        // :72
    auto csrfSecret = Config_->GetCsrfSecret();   // :74
    auto csrfToken  = SignCsrfToken(result.Value().Result.Login, csrfSecret, TInstant::Now());  // :75
    ReplyJson(rsp, ... .Item("login")      .Value(result.Value().Result.Login)      // :80
                       .Item("realm")      .Value(result.Value().Result.Realm)      // :81
                       .Item("real_login") .Value(GetRealLogin(result.Value().Result))  // :82
                       .Item("csrf_token") .Value(csrfToken));                       // :83
} else {
    SetStatusFromAuthError(rsp, TError(result));  // :87
    FillYTErrorHeaders(rsp, TError(result));      // :88
    ReplyJson(rsp, ... .Value(TError(result)));   // :89-92
}
```

Success wire:

```http
HTTP/1.1 200 OK
Content-Type: application/json
Pragma: nocache
Expires: Thu, 01 Jan 1970 00:00:01 GMT
Cache-Control: max-age=0, must-revalidate, proxy-revalidate, no-cache, no-store, private
X-Content-Type-Options: nosniff
X-Frame-Options: SAMEORIGIN
X-DNS-Prefetch-Control: off

{"login":"admin","realm":"cypress","real_login":"admin","csrf_token":"9ab…f2:1753440000"}
```

(the six extra headers come from `ProtectCsrfToken`,
`yt/core/http/helpers.cpp:321-331`.)

CSRF token format (`yt/library/auth_server/helpers.cpp:160-164`):

```cpp
auto msg = userId + ":" + ToString(now.TimeT());
return CreateSha256Hmac(key, msg) + ":" + ToString(now.TimeT());
```

i.e. `hex(hmac_sha256(secret, "<login>:<unixtime>")) + ":" + <unixtime>`. The
secret is read **only** from `blackbox_cookie_authenticator/csrf_secret`
(`yt/library/auth_server/config.cpp:519-528`) and is the empty string in a
typical OSS Cypress-cookie deployment.

The UI only reads `login` and `csrf_token`
(`ui/src/server/components/cluster-queries.ts:53-54`); `realm` / `real_login` are
ignored.

### 4.3 `/login` (Cypress cookie login)

`TCypressCookieLoginHandler`, `yt/library/auth_server/cypress_cookie_login.cpp:35-243`.

* Any HTTP method. Branches on presence of `Authorization` (`:50-62`).
* **No** `Authorization` ⇒ `401` + `WWW-Authenticate: Basic`, empty body (`:224-228`).
* `Authorization` must be `Basic base64(user:password)` (`:98-131`); otherwise
  `400` with `Malformed "Authorization" header: …` / `Unsupported authorization
  method %Qlv` / `Failed to parse user credentials`.
* **No JSON body is ever parsed.** There is no `_login_password_` command.
* Password check (`yt/library/auth_server/cypress_login_authenticator.cpp:39-64`):
  reads `//sys/users/<user>/@hashed_password` and `@password_salt`, compares with
  `HashPassword(password, salt) = sha256_hex(salt + sha256_hex(password))`
  (`yt/core/crypto/crypto.cpp:260-270`).
* Bad credentials ⇒ `401` + `WWW-Authenticate: Basic` + masked body
  `{"code":1,"message":"Incorrect login or password",…}` (`:82-84,137-164`).
* Success (`:166-222`): 32 random bytes hex-encoded → 64-char cookie value
  (`cypress_cookie.cpp:47-53`), stored at `//sys/cypress_cookies/<value>`
  (`cypress_cookie_store.cpp:219,282`) with `{value,user,password_revision,auth_source,expires_at}`.
  Status `200 OK`, or `301` + `Location:` if `cookie_generator/redirect_url`
  is set (`:214-219`). **Body is empty** (`:60-61`).
* `Set-Cookie` is assembled by `TCypressCookie::ToHeader`,
  `yt/library/auth_server/cypress_cookie.cpp:15-33`, in exactly this order:

  ```
  YTCypressCookie=<64 hex chars>; Expires=<RFC822>[; Secure][; HttpOnly][; Domain=<d>]; Path=<p>
  ```

  * name constant `CypressCookieName = "YTCypressCookie"` — `yt/library/auth_server/public.h:139`
  * `Secure` iff `cookie_generator/secure` (default **true**, `config.cpp:448-449`)
  * `HttpOnly` iff `cookie_generator/http_only` (default **true**, `config.cpp:451-452`)
  * `Domain=` only when configured (default unset, `config.cpp:454-455`)
  * `Path=` always, default `/` (`config.cpp:456-457`)
  * **never any `SameSite`**
  * lifetime = `cookie_expiration_timeout` (default 90 days), or
    `ldap_cookie_expiration_timeout` (default 8h) for LDAP logins.

Concrete example (matches `yt/tests/integration/proxies/test_cypress_cookie_auth.py:110-129`):

```http
POST /login HTTP/1.1
Authorization: Basic YWRtaW46c2VjcmV0

HTTP/1.1 200 OK
Set-Cookie: YTCypressCookie=51f3…8b; Expires=Wed, 23 Oct 2026 12:00:00 GMT; Secure; HttpOnly; Path=/
```

### 4.4 Credential resolution for `/api/v3|v4/*`

`TContext::TryParseUser` (`yt/server/http_proxy/context.cpp:199-254`) calls
`THttpAuthenticator::Authenticate(request)` with the default
`disableCsrfTokenCheck = false`. Order inside
`http_authenticator.cpp:96-280`:

1. **Auth disabled** (`:101-113`): if `!Config_->RequireAuthentication`,

   ```cpp
   auto user = NRpc::RootUserName;                                  // "root"
   if (auto h = request->GetHeaders()->Find("X-YT-User-Name")) user = *h;   // unvalidated!
   return TAuthenticationResult{.Login = user, .Realm = "YT", .UserTicket = ""};
   ```

   Cookies and tokens are never looked at.
2. `Authorization:` (`:126-199`) — must start with `OAuth ` or `Bearer `, else
   `InvalidCredentials "Malformed Authorization header"`. Non-empty token goes to
   the token authenticator. `X-YT-User-Name` alongside a token = impersonation,
   allowed only for superusers or the hardcoded whitelist `{"yql_agent"}`
   (`:128,163-194`); realm becomes `<realm>:impersonation`.
   An *empty* token falls through to the cookie branch.
3. `Cookie:` (`:201-237`) — parsed by `ParseCookies`; the composite cookie
   authenticator claims the request if any of `YTCypressCookie`, `Session_id`,
   `sessionid2`, `sessguard`, `access_token`, `yc_session` is present
   (`yt/library/auth_server/public.h:136-141`). Then:

   ```cpp
   if (request->GetMethod() != EMethod::Get && !disableCsrfTokenCheck) {   // :214
       constexpr TStringBuf CrfTokenHeaderName = "X-Csrf-Token";           // :215
       if (!csrfTokenHeader) return TError(InvalidCredentials, "CSRF token is missing");  // :217-221
       auto error = CheckCsrfToken(Strip(...), authResult.Value().Login,
                                   Config_->GetCsrfSecret(), Config_->GetCsrfTokenExpirationTime()); // :223-227
       if (!error.IsOK() && !dynamicConfig->RelaxCsrfCheck) return error;  // :230-232
   }
   ```

   So **CSRF is required only for cookie auth on non-GET methods**, and can be
   made advisory by the dynamic knob `relax_csrf_check`
   (`yt/server/http_proxy/config.h:404`, default false).
   `CheckCsrfToken` (`yt/library/auth_server/helpers.cpp:166-193`) rejects with
   `TError("Malformed CSRF token")` (code 1 ⇒ **503**, not 401), or
   `InvalidCsrfToken` for "CSRF token expired" / "Invalid CSFR token signature"
   (typo is in the source).
   Cypress cookie validation additionally fails if the stored
   `password_revision` no longer matches (`cypress_cookie_authenticator.cpp:120-125`)
   or the cookie expired (`:128-132`); unknown cookie ⇒ `InvalidCredentials
   "Unknown credentials"` (`:71-80`). Near expiry the authenticator can hand back
   a refreshed cookie which `TryParseUser` re-emits as `Set-Cookie`
   (`context.cpp:225-227`).
4. `X-Ya-User-Ticket` (`:239-256`) and `X-Ya-Service-Ticket` (`:258-275`) — TVM.
5. Fallthrough ⇒ `InvalidCredentials "Client is missing credentials"` (`:277-279`).

After authentication: `discover_proxies` bypasses auth and runs as `root`
(`context.cpp:208-211`); `ping_tx`/`parse_ypath` skip user validation
(`:229-233`); `ValidateUser` failure ⇒ **403** "User validation failed"
(`:235-241`); `CheckAccess` failure ⇒ **403** `User %Qv is not allowed to access
proxy with role %Qv` (`:243-249`, `access_checker.cpp:41-74`).

CORS: `Access-Control-Allow-Headers`/`Expose-Headers` include `Authorization`
and `X-Csrf-Token`, with `Access-Control-Allow-Credentials: true`
(`yt/core/http/helpers.cpp:174-206,276-280`). `/login` is intentionally **not**
CORS-enabled.

### 4.5 Error shape on 401

`SetStatusFromAuthError` (`http_authenticator.cpp:39-50`) yields **401** only for
`NRpc::EErrorCode::InvalidCredentials` (111), `NRpc::EErrorCode::InvalidCsrfToken`
(110) and `NSecurityClient::EErrorCode::AuthenticationError` (900). **Every other
error becomes 503**, which is a common surprise when mocking.

`FillYTErrorHeaders` → `FillYTError` (`yt/core/http/helpers.cpp:51-74`, names at
`helpers.h:56-59`) adds:

```
X-YT-Error: {"code":111,"message":"Client is missing credentials","attributes":{…}}
X-YT-Error-Content-Type: application/json
X-YT-Response-Code: 111
X-YT-Response-Message: Client is missing credentials
```

Body is the same serialized `TError` as JSON (`yt/core/misc/error.cpp:329,363-373`):

```json
{
  "code": 111,
  "message": "Client is missing credentials",
  "attributes": {"pid": 1, "tid": 1, "thread": "…", "fid": 1, "host": "…",
                 "datetime": "2026-07-25T12:00:00.000000Z", "trace_id": "…", "span_id": "…"},
  "inner_errors": []
}
```

Full 401 example:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
X-YT-Error: {"code":111,"message":"Client is missing credentials","attributes":{},"inner_errors":[]}
X-YT-Error-Content-Type: application/json
X-YT-Response-Code: 111
X-YT-Response-Message: Client is missing credentials

{"code":111,"message":"Client is missing credentials","attributes":{},"inner_errors":[]}
```

For non-auth command failures `TContext::Finalize` (`context.cpp:1104-1135`)
forces **`400 Bad Request`** and writes `X-YT-Error`, `X-YT-Error-Content-Type`,
`X-YT-Response-Code`, `X-YT-Response-Message` (`formats.cpp:208-244`); if headers
were already flushed the same fields go into trailers announced up-front by
`Trailer: X-YT-Error, X-YT-Response-Code, X-YT-Response-Message` (`context.cpp:846`).

`WWW-Authenticate` is emitted only by the `/login` handler
(`cypress_cookie_login.cpp:227`); `TContext::DispatchUnauthorized`
(`context.cpp:1154-1159`) exists but is never called. And recall the UI's
`pipeResponse` strips `www-authenticate` anyway
(`ui/src/server/utils/index.ts:144`), so the browser never sees a native
Basic-auth prompt.

### 4.6 `whoami`-style driver commands

Registered for both v3 and v4 in `yt/client/driver/driver.cpp:370-375`:

| command | file | params | result |
|---|---|---|---|
| `get_current_user` | `yt/client/driver/etc_commands.h:36-48` | none | `{"user":"<name>"}` (`yt/client/api/security_client.cpp:9-16`) |
| `set_user_password` | `yt/client/driver/authentication_commands.cpp:13-36` | `user`, `current_password_sha256` (default ""), `new_password_sha256`, `password_is_temporary` (default false) | empty |
| `issue_token` | `authentication_commands.cpp:41-62` | `user`, `password_sha256` (default ""), `description?` | the token string as a scalar |
| `revoke_token` | `authentication_commands.cpp:67-84` | `user`, `password_sha256` (default ""), `token_sha256` | empty |
| `list_user_tokens` | `authentication_commands.cpp:89-114` | `user`, `password_sha256` (default ""), `with_metadata` (default false) | list of sha256 hashes, or `{sha256: metadata}` when `with_metadata` |

The UI uses `set_user_password` (change-password, §3.2) and
`issue_token` / `revoke_token` / `list_user_tokens` (the "Manage tokens" dialog,
`ui/src/ui/store/actions/manage-tokens/index.ts:38-98`; wrapper command table
`jsw/lib/commands/v4.js:160-175` — note all three are declared `method: 'GET'`
there, so they never trigger the proxy's CSRF requirement).
`get_current_user` is **not** used by the UI at all — the UI's "who am I" is
`/auth/whoami` via the Node server, because that is also the only endpoint that
mints the CSRF token.

### 4.7 Proxy config knobs that disable authentication

| knob | default | effect |
|---|---|---|
| `auth/require_authentication` (alias `enable_authentication`) | **true** (`yt/library/auth_server/config.h:624`, `config.cpp:542-544`) | when false ⇒ every API request is `root`, or whatever `X-YT-User-Name` says; realm `"YT"`; also installs noop token/RPC authenticators (`authentication_manager.cpp:186-197`) |
| `auth/cypress_cookie_manager` | unset (`config.cpp:562-563`) | gates the cookie authenticator **and** the whole `/login` route |
| `auth/cypress_password_authenticator/enabled` | true (`config.cpp:346-350`) | password login source for `/login` |
| `auth/cypress_token_authenticator` | unset (`config.cpp:552-553`) | enables `//sys/tokens/*` and `//sys/cypress_tokens/*` token auth; realm `"cypress"` |
| `auth/blackbox_cookie_authenticator/csrf_secret` + `csrf_token_ttl` | unset / default TTL (`config.cpp:161-164,519-535`) | the only source of the CSRF signing key |
| dynamic `relax_csrf_check` | false (`yt/server/http_proxy/config.h:404`) | makes CSRF failures non-fatal |
| `access_checker/enabled` | — | role-based `403` (`access_checker.cpp:44-47`) |

Note the asymmetry: even with `require_authentication: false` the proxy still
answers `/auth/whoami` with `{"login":"root","realm":"YT","real_login":"root","csrf_token":"…"}`,
so the UI happily reports the user as `root`.

---

## 5. Minimal mock backend

### 5.1 Recommended shape: no authentication at all

The cheapest correct configuration is to make the UI never ask about auth:

1. UI server env: do **not** set `ALLOW_PASSWORD_AUTH`, `WITH_AUTH` or
   `YT_AUTH_CLUSTER_ID`; do not configure `ytOAuthSettings`.
   ⇒ `allowPasswordAuth = false`, `appAuthPolicy = AuthPolicy.disabled`
   (`apply-app-env-to-config.ts:32,37`), no auth middleware at all
   (`server/index.ts:34-45`).
2. `clusters-config.json`:

   ```json
   {"clusters": [{"id": "mock", "name": "Mock", "proxy": "127.0.0.1:8000",
                  "secure": false, "authentication": "none",
                  "theme": "mint", "environment": "development",
                  "description": "Mock backend"}]}
   ```

   `authentication: "none"` ⇒ `getUserYTApiSetup` / `getRobotYTApiSetup` attach
   **no** credential header (`requestsSetup.ts:90-95,122-124`), and the browser's
   javascript-wrapper sets `withCredentials: false` (`jsw/lib/core.js:246`).

With that, the mock proxy needs **zero** auth endpoints. Required non-auth
endpoints (all unauthenticated) are:

| method | path | why | response |
|---|---|---|---|
| GET | `/version` | `getVersions`, `getClusterInfo` (`cluster-queries.ts:72-81,122-155`) | `text/plain` body like `24.1.0-mock` (the UI regexes `(\d+)\.(\d+)\.(\d+)`) |
| GET | `/auth/whoami` | still called by `getClusterInfo` even with auth off — see 5.2 | `{"login":"root","realm":"YT","real_login":"root","csrf_token":"mock:9999999999"}` |
| POST | `/api/v3/execute_batch` | `/api/cluster-params/:cluster` | batch results for `list //sys/media`, `get //sys/scheduler/orchid/service/version`, `get //sys/@ui_config`, `get //sys/@ui_config_dev_overrides`, `list //sys/primary_masters` (+ its version) |
| GET/POST | `/api/v4/<command>` | everything the pages do | per-command |
| GET | `/hosts` | heavy commands (`yt-api.ts:94-100`) | `["127.0.0.1:8000"]` |
| GET | `/hosts/all`, `/internal/discover_versions/v2` | only if the components pages are opened (`yt-proxy-api.ts:21-24`) | — |

**`/auth/whoami` is mandatory even with `authentication: "none"`.** It is called
unconditionally from `getClusterInfo` (`cluster-queries.ts:128-131`), and if it
fails the client sets `PRELOAD_ERROR.AUTHENTICATION` and refuses to render the
cluster (`cluster-params.ts:249-257`). The mock may ignore all credentials and
always return a fixed body.

Minimal `/auth/whoami` response that satisfies the UI (only `login` and
`csrf_token` are read):

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"login":"root","realm":"YT","real_login":"root","csrf_token":"mock-csrf:9999999999"}
```

The client will then set the browser cookie
`ytfront_mock_xsrf_token=mock-csrf:9999999999` and send
`X-Csrf-Token: mock-csrf:9999999999` on non-GET cluster calls — the mock can
ignore the header entirely.

Note the username shortcut: if the UI runs in local mode
(`APP_ENV=local` / `YT_LOCAL_CLUSTER_ID`), `home.ts:29` hard-codes
`login = 'root'` regardless of anything the backend says.

### 5.2 If you want to exercise the login form

Set `ALLOW_PASSWORD_AUTH=1` (and `YT_AUTH_ALLOW_INSECURE=1` when the UI is served
over plain `http://`, otherwise the `Secure` cookie is dropped —
`login.ts:89-118`), and set `"authentication": "basic"` in `clusters-config.json`.
Then the mock proxy must implement exactly four things:

**(a) `POST /login`** — accept `Authorization: Basic base64(user:pass)`, reply:

```http
HTTP/1.1 200 OK
Content-Length: 0
Set-Cookie: YTCypressCookie=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef; Expires=Fri, 23 Oct 2026 12:00:00 GMT; HttpOnly; Path=/
```

Omit `Secure` when serving the UI over `http://` (or rely on
`ytAuthAllowInsecure` to strip it). Never emit `SameSite` — the real proxy does
not. On bad credentials:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
WWW-Authenticate: Basic

{"code":1,"message":"Incorrect login or password","attributes":{},"inner_errors":[]}
```

The UI relays that to the browser and `LoginFormPage.tsx:82-90` shows
`Incorrect login or password`.

**(b) `GET /auth/whoami`** — must return `200` when
`Cookie: YTCypressCookie=<the value issued above>` is present, and a `401`
(with the YT-error headers/body of §4.5) otherwise. This single endpoint decides
whether the UI Node server considers the session valid
(`middlewares/authorization.ts:23-38`).

**(c) API requests carrying `Cookie: YTCypressCookie=…`** — the mock can ignore
the cookie value and the `X-Csrf-Token` header.

**(d) A `401` shape the UI can act on** — the Node server relays the proxy's
`401` and adds `x-yt-ui-cluster-name: <cluster>` itself
(`yt-auth.ts:13`), which is what makes the browser re-open the login form
(`entries/main.tsx:20-27`, `global/index.ts:314-333`).

Optional extras: `POST /api/v4/set_user_password` for the change-password page,
and `GET /api/v4/{list_user_tokens,issue_token,revoke_token}` for the token
dialog.

### 5.3 Things a mock must NOT do

* Do **not** emit `SameSite=Strict` on `YTCypressCookie` — the login POST is
  same-origin to the UI, but the cookie is re-issued by the UI server for a
  different name; `Strict` breaks the follow-up navigation.
* Do **not** return `403`/`500` for bad credentials: the UI only recognises
  `401` (`authorization.ts:15`, `entries/main.tsx:21`). A `503` (which is what
  the real proxy returns for *non*-`InvalidCredentials` errors, §4.5) will not
  open the login form.
* Do **not** forget `Content-Type: application/json` on error bodies — the
  client reads `error.response.data.message`.
* Do **not** return a `csrf_token` of `undefined`/`null` from `/auth/whoami`:
  `cluster-params.ts:249` treats a falsy `csrf_token` as an authentication
  failure and blocks the whole cluster page.
* Do **not** rely on `www-authenticate` reaching the browser — the UI strips it
  (`ui/src/server/utils/index.ts:144`).

### 5.4 End-to-end trace of a successful password login (mock)

```
1. GET  /                                          → UI server → HTML, window.YT.parameters.login = undefined
                                                     __DATA__.allowPasswordAuth = true  ⇒ login form
2. POST /api/yt/mock/login  {"username":"admin","password":"secret"}
     → POST http://mock:8000/login  Authorization: Basic YWRtaW46c2VjcmV0
     ← 200  Set-Cookie: YTCypressCookie=51f3…; Expires=…; HttpOnly; Path=/
   ← 200  Set-Cookie: YTCypressCookie=51f3…            (relayed)
          Set-Cookie: mock_YTCypressCookie=51f3…       (added by login.ts:57-63)
3. GET  /api/cluster-info/mock       Cookie: mock_YTCypressCookie=51f3…
     → GET http://mock:8000/auth/whoami   Cookie: YTCypressCookie=51f3…
     ← 200 {"login":"admin","realm":"cypress","real_login":"admin","csrf_token":"9ab…:1753440000"}
     → GET http://mock:8000/version   ← "24.1.0-mock"
   ← 200 {"token":{"login":"admin","csrf_token":"9ab…:1753440000"},"version":"24.1.0-mock"}
   browser: document.cookie += "ytfront_mock_xsrf_token=9ab…:1753440000"
4. GET  /api/cluster-params/mock     → POST http://mock:8000/api/v3/execute_batch  (robot creds / none)
5. POST /api/yt/mock/api/v4/list?…   X-Csrf-Token: 9ab…:1753440000
     → POST http://mock:8000/api/v4/list   Cookie: YTCypressCookie=51f3…
                                           X-Csrf-Token: 9ab…:1753440000
                                           X-YT-Suppress-Redirect: 1
```

---
