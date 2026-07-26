# YTsaurus UI ↔ cluster wire protocol: Navigation + static Table viewer

Scope: everything a **mock backend** needs to make the `ytsaurus-ui` frontend boot on a cluster,
browse the Cypress navigation tree, and open one **static** table (schema + rows + meta).

Out of scope (deliberately): operations/scheduler, accounts, dynamic-table mount operations,
CHYT, queries page, system pages, ACL editing, uploads.

Source trees referenced:

| Alias | Path |
|---|---|
| **UI** | `/shared/ytsaurus4/iceberg-viewer/ytsaurus-ui/packages/ui` |
| **WRAP** | `/shared/ytsaurus4/iceberg-viewer/ytsaurus-ui/packages/javascript-wrapper` |
| **HELP** | `/shared/ytsaurus4/iceberg-viewer/ytsaurus-ui/packages/interface-helpers` |
| **YT** | `/shared/ytsaurus4/yt` |

All `file:line` refs below are relative to those roots.

---

## 0. Topology — who calls whom

```
 browser (React app)
   │
   ├─(A) axios, relative URLs ───────────────► UI Node server (express)   ── REST, JSON
   │       /api/cluster-info/<cluster>            │
   │       /api/cluster-params/<cluster>          │  server-side yt.v3.executeBatch
   │       /api/settings/<cluster>/<user>...      ├──────────────────────► cluster HTTP proxy
   │       /api/clusters/versions                 │                        /api/v3/execute_batch
   │       /api/table-column-preset/...           │                        /auth/whoami, /version
   │                                              │
   └─(B) @ytsaurus/javascript-wrapper ────────►   │  reverse proxy (streaming)
           https://<ui-host>/api/yt/<cluster>     └──────────────────────► cluster HTTP proxy
                        /api/v3/<command>            GET|POST|PUT            /api/v3/<command>
```

### (A) vs (B): the decisive switch is `ytApiUseCORS`

`UI/src/ui/store/selectors/global/cluster.ts:40-46`

```ts
export function getClusterProxy(clusterConfig: ClusterConfig): string {
    const allowYtTvmApi = !getConfigData().ytApiUseCORS;
    if (allowYtTvmApi) {
        return `${window.location.host}/api/yt/${clusterConfig.id}`;
    }
    return clusterConfig.proxy;
}
```

This value is installed as the javascript-wrapper's `proxy` global option
(`UI/src/ui/common/yt-api.ts:20`). Therefore:

* **`ytApiUseCORS` falsy (the default)** — every YT command issued through the
  javascript-wrapper goes to the UI Node server at
  `/api/yt/<cluster>/api/v3/<command>`, which reverse-proxies it to the cluster HTTP
  proxy. Normal navigation and table-display requests therefore never talk to the
  cluster proxy directly. The table-download URL can still bypass this tunnel when
  `uiSettings.directDownload` is enabled (§6.4). This is the mode a mock backend should
  target: implement one HTTP origin.
* **`ytApiUseCORS` true** — the wrapper's `proxy` becomes `clusterConfig.proxy` and the browser
  hits `<http|https>://<cluster-proxy>/api/v3/<command>` cross-origin, according to the
  cluster's `secure` setting. This requires the cluster proxy CORS config to allow the UI
  origin (see §3.8).

`ytApiUseCORS` is injected into the page by the Node server:
`UI/src/server/components/layout-config.ts:22,65` → `window.__DATA__.ytApiUseCORS`;
type at `UI/src/shared/yt-types.d.ts:508`.

### The UI Node server's YT reverse proxy

Routes — `UI/src/server/routes.ts:82-84`:

```
GET  /api/yt/:ytAuthCluster/api/:version/:command
POST /api/yt/:ytAuthCluster/api/:version/:command
PUT  /api/yt/:ytAuthCluster/api/:version/:command
```

Handler `ytTvmApiHandler` — `UI/src/server/controllers/yt-api.ts:31-141`. It:

1. Validates `:version` ∈ {v2,v3,v4} and `:command` against the wrapper's own command list
   (`yt.getSupportedCommands()`, `WRAP/lib/index.js:3-13`), plus a hard-coded allowlist
   `{get_job_stderr, get_job_input, get_job_fail_context}` (`yt-api.ts:27`). Unknown → 400.
2. If the command is `heavy` **and** the cluster is not local **and** `disableHeavyProxies` is
   false: `GET http(s)://<proxy>/hosts`, takes `res.data[0]` as the target host
   (`yt-api.ts:92-101`). `read_table` **is** heavy (`WRAP/lib/commands/v3.js:99`), so the mock
   must answer `/hosts` if it wants to exercise this path — or set `disableHeavyProxies`.
3. Forwards `req.method`, the query string (`qs.stringify(req.query)`), the body, and **all**
   incoming headers except `host`, `cookie`, and the UI-only `X-Custom-Request-Id`, plus:
   ```
   accept-encoding: gzip
   X-YT-Correlation-Id: <express request id>
   X-YT-Suppress-Redirect: 1
   Cookie: YTCypressCookie=<secret>     (cookie auth)
     or    access_token=<oauth token>   (OAuth; omitted when authentication == 'none')
   ```
   (`yt-api.ts:104-125`). Response is streamed back verbatim (`responseType: 'stream'`,
   `pipeAxiosResponse`), so **all `X-YT-*` response headers and the body reach the browser
   unchanged**.

Auth header derivation — `UI/src/server/middlewares/yt-auth.ts:6-24` and
`UI/src/server/components/requestsSetup.ts:118-132`: for cookie auth, the browser cookie
`<cluster>_YTCypressCookie` is rewritten into an outgoing `Cookie: YTCypressCookie=<secret>;`
header. OAuth instead supplies `Cookie: access_token=<oauth token>`
(`UI/src/server/middlewares/oauth.ts:14-24`). Both are omitted when
`clusterConfig.authentication === 'none'`.

### Other UI Node endpoints that reach the cluster

| UI Node endpoint | Cluster proxy calls | Source |
|---|---|---|
| `GET /api/cluster-info/:cluster` | `GET <proxy>/auth/whoami`, `GET <proxy>/version` | `UI/src/server/components/cluster-queries.ts:25-70, 72-81, 122-155` |
| `GET /api/cluster-params/:cluster` | 2× `POST /api/v3/execute_batch` | `UI/src/server/components/cluster-params.ts:60-201` |
| `GET|POST /api/settings/:cluster/:user`; `GET|PUT|DELETE /api/settings/:cluster/:user/:path` | `create`/`get`/`set`/`remove` on a Cypress document | `UI/src/server/components/settings.ts:38-130` |
| `GET /api/clusters/versions` | `GET <proxy>/version` per cluster | `UI/src/server/controllers/clusters.ts:6-12`, `cluster-queries.ts:88-100` |
| `GET /api/yt-proxy/:cluster/:command` | `/hosts/all`, `/internal/discover_versions/v2` (allowlist) | `UI/src/server/controllers/yt-proxy-api.ts:21-24` |
| `POST /api/yt/:cluster/login` | `POST <proxy>/login` with `Authorization: Basic ...` | `UI/src/server/controllers/login.ts:23-87` |

---

## 1. javascript-wrapper → proxy: request encoding

Everything here is `WRAP/lib/core.js` + `WRAP/lib/utils/setup.js` + `WRAP/lib/commands/*.js`.

### 1.1 URL construction

`WRAP/lib/core.js:197-211`

```js
core._prepareURL = function (localSetup, command) {
    const protocol = setup.getOption(localSetup, 'secure') ? 'https://' : 'http://';
    const apiVersion = config.version;
    const useHeavyProxy = config.heavy && setup.getOption(localSetup, 'useHeavyProxy');
    const proxy = useHeavyProxy ? setup.getOption(localSetup, 'heavyProxy')
                                : setup.getOption(localSetup, 'proxy');
    return protocol + proxy + '/api/' + apiVersion + '/' + config.name;
};
```

So: `<http|https>://<proxy>/api/<v2|v3|v4>/<snake_case_command>`.

**Important quirk:** the api version comes from `config.version` *if the command config sets it*,
otherwise from the namespace the caller used. `WRAP/lib/core.js:376`:

```js
const config = Object.assign({version: version, command: name}, configuration);
```

`configuration` is applied **last**, so a command config carrying `version: 'v3'` pins the version.
`readTable`, `writeTable`, `writeFile` all hard-pin `version: 'v3'`
(`WRAP/lib/commands/v3.js:98, 80, 115`). **`yt.v4.readTable(...)` still issues
`POST /api/v3/read_table`.** The UI calls `ytApiV3.readTable` anyway.

### 1.2 Method + where parameters live

`WRAP/lib/commands/v3.js` (v4 re-exports these — `WRAP/lib/commands/v4.js:18-33`):

| wrapper method | command | method | `dataType` | `useBodyForParameters` | `heavy` |
|---|---|---|---|---|---|
| `get` | `get` | POST | json | ✅ | – |
| `list` | `list` | POST | json | ✅ | – |
| `exists` | `exists` | POST | json | ✅ | – |
| `executeBatch` | `execute_batch` | POST | json | ✅ | – |
| `checkPermission` | `check_permission` | POST | json | ✅ | – |
| `readTable` | `read_table` | POST | text | ✅ | ✅ |
| `selectRows` | `select_rows` | POST | text | ✅ | ✅ |
| `getSupportedFeatures` | `get_supported_features` | GET | json | – | – |
| `readFile` | `read_file` | GET | text | – | ✅ |
| `set` | `set` | PUT | text | – | – |
| `create`/`remove`/`copy`/`move`/`link` | … | POST | text | – | – |

(`v3.js:35-55` for get/list/exists, `:94-106` readTable, `:334-339` executeBatch,
`:346-350` getSupportedFeatures, `v2.js:50-54` set.)

`useBodyForParameters: true` ⇒ parameters are the **JSON request body**, and **no
`X-YT-Parameters*` header is sent** — `WRAP/lib/core.js:248-289`:

```js
headers: core._prepareHeaders(localSetup, command,
    useBodyForParameters ? undefined : preparedParameters),
...
if (useBodyForParameters && preparedParameters) {
    requestParameters.data = setup.encodeForYt(serializer.stringify(preparedParameters));
}
```

`setup.encodeForYt = (str) => unescape(encodeURIComponent(str))` (`WRAP/lib/utils/setup.js:144-146`)
— i.e. UTF-8 bytes carried as a latin1 JS string, so axios ships raw UTF-8.

### 1.3 Headers

`WRAP/lib/core.js:154-187`:

```js
const headers = Object.assign({}, setup.getOption(localSetup, 'requestHeaders'), {
    Accept: 'application/json',
});
if (authentication.type === 'oauth' && authentication.token) {
    headers['Authorization'] = 'OAuth ' + authentication.token;
}
if (parameters) { appendParametersHeaders(headers, localSetup, parameters); }
if (config.method === 'PUT' || config.method === 'POST') {
    headers['Content-Type'] = 'application/json';
}
const useHeavyProxy = config.heavy && setup.getOption(localSetup, 'useHeavyProxy');
if (!useHeavyProxy) { headers['X-YT-Suppress-Redirect'] = 1; }
```

`appendParametersHeaders` — `WRAP/lib/core.js:70-81` (only reached when
`useBodyForParameters` is falsy):

```js
if (encode /* useEncodedParameters, default true */) {
    appendEncodedParameters(headers, localSetup, parameters);
    headers['X-YT-Header-Format'] = '<encode_utf8=%false>json';
} else {
    headers['X-YT-Parameters'] = serializer.stringify(parameters);
}
```

`appendEncodedParameters` — `WRAP/lib/core.js:48-68`: base64 of
`unescape(encodeURIComponent(JSON.stringify(params)))`, split into ≤ 64 KiB chunks, emitted as
`X-YT-Parameters-0`, `X-YT-Parameters-1`, … (max 2 chunks by default —
`WRAP/lib/utils/setup.js:148-155`). The proxy understands both spellings (§3.3).

`X-Custom-Request-Id` is added by the UI's per-call wrapper as a *purely diagnostic* tag
(`UI/src/shared/constants/index.ts:1` = `YT_API_REQUEST_ID_HEADER`,
`UI/src/ui/rum/rum-wrap-api.ts:296-299`). The Node server strips it before forwarding and
uses it for stats only (`UI/src/server/controllers/yt-api.ts:25,64,82,107`).

### 1.4 Global options installed by the UI

`UI/src/ui/common/yt-api.ts:14-42`:

| option | value | consequence |
|---|---|---|
| `suppressAccessTracking` | `true` | adds `suppress_access_tracking: 'true'` to `get`/`list`/`exists`/`read_table`/`select_rows` params (`WRAP/lib/commands/v3.js:6-14`) |
| `useEncodedParameters` | `true` | base64 `X-YT-Parameters-N` for non-body commands |
| `proxy` | `<ui-host>/api/yt/<cluster>` (default) | see §0 |
| `useHeavyProxy` | **`false`** | browser never calls `/hosts`; `X-YT-Suppress-Redirect: 1` on every request |
| `xsrf` | `true`, cookie `ytfront_<cluster>_xsrf_token` (`UI/src/ui/utils/index.ts:252-254`) | axios adds `X-Csrf-Token` **only when `withCredentials`** |
| `secure` | `window.location.protocol === 'https:'` (TVM mode) | http vs https prefix |
| `authentication` | `{type: clusterConfig.authentication ?? 'none'}` | `withCredentials = type && type !== 'none'` (`WRAP/lib/core.js:246`) |

Note: with `authentication: 'none'`, `withCredentials` is false, hence
`withXSRFToken` is false and **no `X-Csrf-Token` is sent**. Same-origin cookies still flow.

### 1.5 Navigation attributes (`execute_batch`)

The exact navigation batch and attribute set are in §6.2. Its typed response
uses the conventions in §4.1; v3/v4 batch envelopes are compared in §4.2.

### 1.6 `read_table` requests

The preload and page requests are specified in §6.4; the response body is
defined in §5.2.

Note `read_table`'s output **MIME is `application/json`** because `FormatToMime(WebJson)`
returns `application/json` (`YT/yt/server/http_proxy/formats.cpp:183-184`), yet the wrapper's
`dataType: 'text'` means axios keeps it as a string; the UI JSON-parses it itself
(`UI/src/ui/utils/navigation/content/table/table.js:29`).

---

## 2. Command semantics from the driver (parameters the mock must honour)

`YT/yt/client/driver/driver.cpp:142-149` defines the descriptor macro
`REGISTER(command, name, inDataType, outDataType, isVolatile, isHeavy, version)`.

| command | in | out | volatile | heavy | line |
|---|---|---|---|---|---|
| `get` | Null | Structured | false | false | `driver.cpp:164` |
| `list` | Null | Structured | false | false | `driver.cpp:165` |
| `exists` | Null | Structured | false | false | `driver.cpp:174` |
| `execute_batch` | Null | Structured | true | false | `driver.cpp:338` |
| `get_table_columnar_statistics` | Null | Structured | false | **true** | `driver.cpp:196` |
| `read_table` | Null | **Tabular** | false | **true** | `driver.cpp:197` |

`OutputType` matters: it drives the default output format (§3.5) and whether the proxy buffers
the response in memory or streams it (`YT/yt/server/http_proxy/context.cpp:697-712`).

### 2.1 `get`

`YT/yt/client/driver/cypress_commands.cpp:23-59` — parameters:
`path` (required), `attributes` (TAttributeFilter; **default = universal filter, i.e. all**),
`max_size`, `return_only_value` (default `false`), `node_count_limit`, `result_size_limit`.
Unrecognized parameters are kept and forwarded (`TCommandBase::Register`,
`YT/yt/client/driver/command.cpp:64-67` sets `EUnrecognizedStrategy::Keep`), so
`transaction_id`, `suppress_access_tracking`, `suppress_upstream_sync`, `timeout` all pass through.

Output: `ProduceSingleOutputValue(context, "value", result)`
(`cypress_commands.cpp:68-73`) → v3 emits the bare value, v4 emits `{"value": …}` (§4.2).

### 2.2 `list`

`cypress_commands.cpp:169-208` — same parameter set, **but** `attributes` defaults to an
*empty* filter, not the universal one.

Output is the list of child keys; when `attributes` is non-empty each entry is a string carrying
`$attributes` (§4.1).

### 2.3 `exists`

`cypress_commands.cpp:652-664` — only `path`. Output is a boolean via `ProduceSingleOutputValue`.

### 2.4 `execute_batch`

`YT/yt/client/driver/etc_commands.cpp:434-441` — parameters `requests` (required),
`concurrency` (default 50, > 0). Sub-request schema `etc_commands.cpp:303-309`:
`{command, parameters, input?}`.

Per-subrequest result (`etc_commands.cpp:418-430`) is `{}`,
`{"output": …}`, or `{"error": {...}}`. **Sub-requests always run
against the driver's own YSON format** — `parameters->Set("output_format", TFormat(EFormatType::Yson))`
(`etc_commands.cpp:380`) — which means the batch's own `output_format` governs the *outer*
envelope only. Commands whose output is `Tabular` or `Binary` (e.g. `read_table`) are **rejected**
inside a batch (`etc_commands.cpp:352-359`).

Top level: `ProduceSingleOutput(context, "results", ...)` (`etc_commands.cpp:462`) → v3 bare list,
v4 `{"results": [...]}`.

### 2.5 `read_table`

`YT/yt/client/driver/table_commands.cpp:75-100` — parameters:

| parameter | default | note |
|---|---|---|
| `path` | required | a **rich YPath**: ranges/columns are encoded either in the string (`//t[#0:#51]`, `//t{"col"}`) or as `$attributes.ranges` |
| `table_reader` | – | UI sends `{"workload_descriptor":{"category":"user_interactive"}}` |
| `control_attributes` | new | `enable_table_index`/`enable_row_index`/`enable_range_index`/`enable_tablet_index` |
| `unordered` | false | |
| `start_row_index_only` | false | if true, only response parameters are produced, no body |
| `omit_inaccessible_columns` | false | UI sends `true` |
| `omit_inaccessible_rows` | false | UI sends `true` |

Plus inherited: `transaction_id` (`command-inl.h:41-47`), `dump_error_into_response`
(read by the proxy, `context.cpp:1093-1097`), `suppress_access_tracking`.

Response parameters (→ `X-YT-Response-Parameters` header) — `table_commands.cpp:132-140`:

```cpp
.Item("approximate_row_count").Value(reader->GetTotalRowCount())
.Item("omitted_inaccessible_columns").Value(reader->GetOmittedInaccessibleColumns())
.DoIf(reader->GetTotalRowCount() > 0, ... .Item("start_row_index").Value(reader->GetStartRowIndex()) )
```

The UI reads only `omitted_inaccessible_columns`
(`UI/src/ui/utils/navigation/content/table/table.js:44-58`).

### 2.6 `get_table_columnar_statistics` — **not used by the UI**

Registered at `driver.cpp:196`, implemented `table_commands.cpp:389-505`. Parameters:
`paths` (required list of rich YPaths), `fetcher_mode` (default `from_nodes`),
`max_chunks_per_node_fetch`, `enable_early_finish` (true), `enable_read_size_estimation` (false).
Output is a **list**, one map per path:
`{column_data_weights:{col:i64}, legacy_chunks_data_weight, timestamp_total_weight?,
column_min_values?, column_max_values?, column_non_null_value_counts?,
column_estimated_unique_counts?, chunk_row_count?, legacy_chunk_row_count?, read_size_estimate?}`
(`table_commands.cpp:458-504`).

A grep over `UI/src/ui` finds **no call site**. A mock backend does not need it.

---

## 3. The HTTP proxy's request handling (what a mock must emulate)

All refs `YT/yt/server/http_proxy/*` unless noted.

### 3.1 Routing

`bootstrap.cpp:623-644` registers, all wrapped in `AllowCors(...)`:

```
/auth/whoami            /api/                  /hosts/
/cluster_connection/    /ping/                 /login/            (if cookie auth enabled)
/internal/discover_versions/v2                 /version           /service
/query  /chyt           /solomon_proxy
```

`/version` returns the plain version string as the body; `/service` returns
`{"start_time":…, "version":…}` (`bootstrap.cpp:467-484`).

API version + command parsing — `context.cpp:138-197`:

* the URL **path is lowercased** first (`context.cpp:140`);
* `/api` or `/api/` ⇒ 200 with body `["v3","v4"]` (`context.cpp:142-152`);
* `/api/v3…` ⇒ version 3, `/api/v4…` ⇒ version 4, anything else **throws**
  `Unsupported API version` which becomes HTTP **400** (`context.cpp:154-160`);
* `/api/v3` or `/api/v3/` ⇒ 200 with the JSON command-descriptor list;
* command name must be `/[A-Za-z_]+` — anything else (including digits) ⇒ **404**
  `Malformed command name` (`context.cpp:182-193`);
* unregistered command ⇒ **404** `Command "…" is not registered` (`context.cpp:264-278`).

**The proxy performs no HTTP-method validation.** The only method test in the whole request
path is `if (Request_->GetMethod() == EMethod::Post)` when deciding whether to read the body as
parameters (`context.cpp:468`). `Volatile`/`InputType`/`OutputType` are never compared against
the method. A mock can therefore accept GET *and* POST for `get`/`list`/`exists` — which is what
the wrapper's v2 (GET) vs v3 (POST) variants rely on.

### 3.2 Header names

`YT/yt/core/http/helpers.h:20-67` defines the canonical constants. Header lookup is
**case-insensitive** (`YT/yt/core/http/http.h:183,215-216`). Headers the proxy reads:

| header | where | semantics |
|---|---|---|
| `X-YT-Header-Format` | `context.cpp:312` → `formats.cpp:109-128` | format used to parse `X-YT-Parameters` / `X-YT-*-Format` / to serialize `X-YT-Response-Parameters`. **Default `json`.** No multi-part support. |
| `X-YT-Parameters` (+ `X-YT-Parameters0/1…`, `X-YT-Parameters-0/-1…`) | `context.cpp:454` | command parameters |
| `X-YT-Input-Format` (+ numbered) | `context.cpp:317-338` | overrides `Content-Type` |
| `X-YT-Output-Format` (+ numbered) | `context.cpp:359-380` | overrides `Accept` |
| `X-YT-Error-Format` (+ numbered) | `context.cpp:402-422` | format for the serialized error in `X-YT-Error`; falls back to json |
| `X-YT-Suppress-Redirect` | `context.cpp:292` | **presence only**, value ignored |
| `X-YT-Accept-Framing` | `context.cpp:130-133` | **presence only**; enables framing (§3.7) |
| `X-YT-Omit-Trailers` | `context.cpp:126-128` | presence only; suppresses the `Trailer:` header |
| `X-YT-Correlation-ID` | `context.cpp:94, 611` | logging only |
| `Content-Type` / `Accept` | `context.cpp:327, 369` | MIME fallback for input/output format |
| `Content-Encoding` / `Accept-Encoding` | `context.cpp:342-397` | unsupported ⇒ **415** |
| `If-None-Match` | `context.cpp:490-502` | ETag; may yield **304** |
| `Authorization` | `http_authenticator.cpp:126-199` | must start with `OAuth ` or `Bearer ` |
| `Cookie` | `http_authenticator.cpp:201-236`, `helpers.cpp:298-301` | cookie auth; also marks the request as a "browser request" which disables heavy redirect |
| `X-Csrf-Token` | `http_authenticator.cpp:215` | required for non-GET cookie-auth requests |

There is **no** `X-YT-User-Agent` and **no** `X-YT-Testing-*` header; testing delays are
config-only (`config.h:142-154`, `context.cpp:1216-1242`).

### 3.3 Multi-part / base64 headers — `GatherHeader`

`helpers.cpp:30-67`:

```cpp
if (auto singleHeader = headers->Find(headerName)) { return *singleHeader; }   // verbatim, NOT base64
std::string buffer;
for (int i = 0; ; i++) {
    if (i > 1000) THROW_ERROR_EXCEPTION("Too many header parts");
    { auto key = Format("%v%v",  headerName, i); if (auto p = headers->Find(key)) { buffer += *p; continue; } }
    { auto key = Format("%v-%v", headerName, i); if (auto p = headers->Find(key)) { buffer += *p; continue; } }
    if (i == 0) return {}; else break;
}
return Base64Decode(buffer);
```

Rules a mock must reproduce:

* `X-YT-Parameters` alone ⇒ used **verbatim** (parsed with the header format).
* `X-YT-Parameters0` / `X-YT-Parameters-0` (and `…1`, `…2`, contiguous from 0) ⇒ concatenated,
  then **base64-decoded**, then parsed. This is the path wrapper commands use when their
  parameters are not body-encoded. The viewer's v3 `get`/`list` and `select_rows` commands
  are body-encoded instead.
* Same mechanism for `X-YT-Input-Format`, `X-YT-Output-Format`, `X-YT-Error-Format`.
* **Not** for `X-YT-Header-Format` (plain `Find`, `context.cpp:312`).

### 3.4 Parameter merge order

`TContext::CaptureParameters`, `context.cpp:437-486`. Each stage is `PatchNode(base, patch)`
— later wins:

1. base map `{input_format: <inferred>, output_format: <inferred>}` (`context.cpp:439-443`);
2. **query string** — `ParseQueryString(RawQuery)` + `FixupNodesWithAttributes`
   (`context.cpp:445-451`). Values that parse as `i64` become integers; nesting via
   `a[b][0][]` up to depth 6, list index ≤ 1024 (`helpers.cpp:69-175`); a top-level value that
   is a map with `$value` is unwrapped and its `$attributes` merged (`helpers.cpp:177-210`);
3. **`X-YT-Parameters` header(s)** parsed with the header format (`context.cpp:453-466`);
4. **POST body** — only if method is POST and the body is non-empty; parsed with the *input*
   format as `Structured` (`context.cpp:468-485`). Non-identity `Content-Encoding` on a POST
   body is an error.

Then `ProcessFormatsInParameters` re-validates the `input_format`/`output_format` parameters
through the format manager (`context.cpp:898-904`).

### 3.5 Format negotiation

`formats.cpp:68-107` (`InferFormat`), precedence:

1. If `target == Output` and the command's `OutputType` is `Null` or `Binary` ⇒ forced `yson`,
   headers ignored (`formats.cpp:78-84`).
2. `X-YT-Input-Format` / `X-YT-Output-Format` header (parsed with the header format).
3. MIME header (`Content-Type` for input, `Accept` for output) — **exact string match** against
   `formats.cpp:29-52`; no `q=` weights, no wildcards. `application/json` → `json`.
4. Default by data type (`formats.cpp:55-64`):
   * `Structured` → `json`
   * `Tabular` → `<format=text>yson`
   * `Null`/`Binary` → `yson`

Reverse mapping for the response `Content-Type` (`formats.cpp:130-193`, applied at
`context.cpp:575-585`): `Json → application/json`, **`WebJson → application/json`**,
yson → `application/x-yt-yson-{text,pretty,binary}`, `Arrow → application/vnd.apache.arrow.stream`.
`OutputType == Binary` overrides this with `application/octet-stream` (or
`text/plain; charset="utf-8"` when `Content-Disposition` starts with `inline`);
`OutputType == Null` emits no `Content-Type` at all.

JSON format attributes (`YT/yt/core/json/config.cpp:7-51`) — relevant defaults:
`format=text`, `attributes_mode=on_demand`, `encode_utf8=true`, `plain=false`,
`stringify=false`, `annotate_with_types=false`, `support_infinity=false`,
`stringify_nan_and_infinity=false`, `string_length_limit` unset.
The UI's `TYPED_OUTPUT_FORMAT` flips `stringify` + `annotate_with_types` on
(`UI/src/ui/constants/index.ts:36-42`).

### 3.6 Response headers

* `X-YT-Proxy: <self host>` and `Cache-Control: no-store` — `helpers.cpp:354-357`,
  `context.cpp:92,98`.
* `Content-Type` when the output type has one; `Content-Encoding` plus
  `Vary: Content-Encoding` only when response compression is active — `context.cpp:830-843`.
* `Trailer: X-YT-Error, X-YT-Response-Code, X-YT-Response-Message` unless
  `X-YT-Omit-Trailers` — `context.cpp:845-847`.
* `X-YT-Response-Parameters` — serialized **in the request's header format** and emitted from
  the response-parameters callback (`context.cpp:1196-1208`). Quirk: for Go clients it is
  suppressed for every command except `read_table` (`context.cpp:1202-1206`).
* `ETag` (and possibly **304**) when the output parameters contain both `id` and `revision`
  (`context.cpp:1183-1194`).
* `Content-Disposition` for heavy commands, filename derived from `path` (`context.cpp:505-573`).

### 3.7 Framing (`framing.cpp`) — exact wire format

```cpp
DEFINE_ENUM_WITH_UNDERLYING_TYPE(EFrameType, uint8_t,
    ((Data)      (0x01))
    ((KeepAlive) (0x02))
);
```
(`framing.cpp:12-15`)

**Data frame** (`framing.cpp:26-38`):

```
byte  0      : 0x01
bytes 1..4   : ui32 payload length, LITTLE-endian   (HostToLittle + WriteUnaligned<ui32>)
bytes 5..5+N : payload
```

Zero-length data frames are legal and are emitted. Payload > `UINT32_MAX` ⇒
`"Data frame is too large: got %v bytes, limit is %v"`.

**Keep-alive frame** (`framing.cpp:40-45`): the **single byte `0x02`** — no length, no payload.

Byte-exact confirmation, `unittests/http_proxy_ut.cpp:19-52`:

```
\x01 \x03\x00\x00\x00 "abc"
\x02
\x02
\x01 \x00\x00\x00\x00
\x01 \x00\x00\x00\x00
\x01 \x0f\x00\x00\x00 "123 456\x00789 ABC"
```

**Negotiation** (`context.cpp:130-133`):

```cpp
if (Request_->GetHeaders()->Find("X-YT-Accept-Framing") && GetFramingConfig()->Enable) {
    Response_->GetHeaders()->Set("X-YT-Framing", "1");
    IsFramingEnabled_ = true;
}
```

The request header's **value is irrelevant**; the response signal is `X-YT-Framing: 1`, and it is
*removed* if the request ends in a pre-flush error (`context.cpp:1115`).

**Layering**: the compression adapter wraps the response stream first, then the framing adapter
wraps the compressor. Driver writes therefore produce `compress(frame(data))`
(`context.cpp:715-725`). Framing also forces the streaming path even for `Structured`/`Null`
output (`context.cpp:699-704`).

**Keep-alive scheduling**: a `TPeriodicExecutor` started only after the driver emits response
parameters (`context.cpp:727-746`, started at `context.cpp:765-768`). Config
`api/framing/{enable, keep_alive_period}` — `enable` default `true`, `keep_alive_period`
default **5 s** (`config.cpp:121-128`); it lives in the **dynamic** config (`config.h:242`).

The UI/javascript-wrapper **never sends `X-YT-Accept-Framing`** (grep over `WRAP/lib` and
`UI/src` finds nothing), so a mock backend for the UI can skip framing entirely.

### 3.8 CORS

`MaybeHandleCors` — `YT/yt/core/http/helpers.cpp:249-293`, wired via `TBootstrap::AllowCors`
(`bootstrap.cpp:609-621`) around every route including `/api/`.

* No `Origin` header ⇒ **nothing emitted at all**.
* Allow iff `disable_cors_check`, or `url.Host` is in `host_allow_list`, or it ends with an entry
  of `host_suffix_allow_list`. Host only — scheme and port are ignored.
  Defaults (`YT/yt/core/http/config.cpp:100-108`):
  `disable_cors_check=false`, `host_allow_list=["localhost"]`,
  `host_suffix_allow_list=[".yandex-team.ru"]`. Config path: `api/cors/…` (static config,
  `YT/yt/server/http_proxy/config.h:221`, `config.cpp:177-178`).
* On allow (`helpers.cpp:275-278`):
  ```
  Access-Control-Allow-Credentials: true
  Access-Control-Allow-Origin: <echoed Origin verbatim>
  Access-Control-Allow-Methods: POST, PUT, GET, OPTIONS
  Access-Control-Max-Age: 3600
  ```
* `OPTIONS` ⇒ also `Access-Control-Allow-Headers: <whitelist>`, status 200, response closed —
  the request never reaches the API handler (`helpers.cpp:280-285`).
* Otherwise ⇒ `Access-Control-Expose-Headers: <same whitelist>` (`helpers.cpp:287`).

The whitelist (`YT/yt/core/http/helpers.cpp:174-206`), verbatim, joined by `", "`:

```
Authorization, Origin, Content-Type, Accept, Cache-Control, Request-Timeout, X-Csrf-Token,
X-YT-Parameters, X-YT-Parameters0, X-YT-Parameters-0, X-YT-Parameters1, X-YT-Parameters-1,
X-YT-Response-Parameters, X-YT-Input-Format, X-YT-Input-Format0, X-YT-Input-Format-0,
X-YT-Output-Format, X-YT-Output-Format0, X-YT-Output-Format-0, X-YT-Error-Format,
X-YT-Header-Format, X-YT-Suppress-Redirect, X-YT-Omit-Trailers, X-YT-Request-Format-Options,
X-YT-Response-Format-Options, X-YT-Request-Id, X-YT-Error, X-YT-Response-Code,
X-YT-Response-Message, X-YT-Trace-Id, X-YT-User-Tag
```

Gaps worth knowing if the mock is used in CORS mode: parameter/format part indices beyond `1`
are **not** whitelisted, and `X-YT-Accept-Framing`, `X-YT-Framing`, `X-YT-Proxy`,
`X-YT-Error-Content-Type`, `ETag` and the UI's own `X-Custom-Request-Id` are absent. In the
default (non-CORS) UI configuration none of this matters because everything is same-origin.

### 3.9 Heavy-command redirect and `X-YT-Suppress-Redirect`

The redirect decision is in `context.cpp:290-308`:

* `CanHandleHeavyRequests()` is `GetSelfEntry()->Role != "control"` (`coordinator.cpp:213-216`),
  so the redirect only ever happens on proxies whose role is literally `"control"`.
* `IsBrowserRequest` is simply "has a `Cookie` header" (`helpers.cpp:298-301`).
* The redirect is **307 Temporary Redirect** with
  `Location: <scheme>://<target-host>:<request-port><path>?<rawQuery>` plus `Connection: close`
  (`helpers.cpp:359-388`). Target role defaults to `"data"`. No data proxies ⇒ **503** +
  `Retry-After: 60`.
* `write_table` (Tabular input) can never be redirected ⇒ **503** on a control proxy.

Because the UI sets `useHeavyProxy: false`, the browser always sends
`X-YT-Suppress-Redirect: 1`; the UI Node server adds it too
(`UI/src/server/controllers/yt-api.ts:119`). **A mock backend never needs to redirect.**

### 3.10 Error propagation

Header/trailer fill — `YT/yt/core/http/helpers.cpp:44-67`:

```
X-YT-Error:              <JSON-serialized TError>
X-YT-Error-Content-Type: application/json      (or FormatToMime(ErrorFormat_))
X-YT-Response-Code:      <decimal error code>
X-YT-Response-Message:   <message, with '\n' escaped to a literal backslash-n>
```

`TContext::Finalize` (`context.cpp:1067-1136`) has two branches:

* **headers not yet flushed** — removes `Trailer`, `Content-Encoding`, `Vary`, `X-YT-Framing`;
  sets status **400 Bad Request for every error** unless an earlier `Dispatch*` already set one
  (`context.cpp:1107-1109`, with a literal `// TODO(prime@): More error codes.`); fills the error
  **headers**; writes the error as a **JSON body**.
* **headers already flushed (streaming)** — status stays **200**, the partial body is kept, and
  the error is emitted as HTTP **trailers**. Clients must read trailers.
* **`dump_error_into_response: true`** (a *parameter*, `context.cpp:1092-1103`) — the error is
  appended into the **response body** between two delimiter lines, `DumpError`
  (`context.cpp:1016-1043`):
  ```
  \n
  ================================================================================\n
  <pretty-printed JSON error>
  \n================================================================================\n
  ```
  The UI relies on exactly this for `read_table`
  (`UI/src/ui/utils/navigation/content/table/table.js:8-26`: it matches an 80-`=` line
  delimiter and requires the trailing delimiter to end the response).

Status code map (per-site, there is no central table):

| status | cause | ref |
|---|---|---|
| 400 | any error surfaced pre-flush | `context.cpp:1108` |
| 401 | `InvalidCredentials`, `InvalidCsrfToken`, `AuthenticationError` | `http_authenticator.cpp:39-50` |
| 403 | user validation / access-checker | `context.cpp:238,245` |
| 404 | malformed or unregistered command | `context.cpp:183,190,273` |
| 304 | ETag match | `context.cpp:1191-1193` |
| 307 | heavy redirect | `helpers.cpp:378` |
| 415 | bad `Content-Encoding` / `Accept-Encoding` | `context.cpp:346,389` |
| 503 | proxy banned, semaphore exhausted, memory pressure, no data proxies | `context.cpp:283,428,953-991`; `helpers.cpp:384` |

Client side, the wrapper turns the parsed JSON body into the rejection value and attaches
request ids (`WRAP/lib/core.js:118-152`). The UI reacts to specific codes
(`UI/src/ui/common/yt-api.ts:44-64`) using `WRAP/lib/commands/codes.js`:
`GENERAL_ERROR=1`, `XSRF_TOKEN_EXPIRED=110`, `NODE_DOES_NOT_EXIST=500`,
`NODE_ALREADY_EXISTS=501`, `NO_SUCH_USER=900`, `PERMISSION_DENIED=901`, `USER_IS_BANNED=903`,
`USER_EXCEEDED_RPS=904`, `NO_SUCH_TRANSACTION=11000`.

---

## 4. Response shapes

### 4.1 YSON → JSON conventions

Writer: `YT/yt/core/json/json_writer.cpp`. Parser: `YT/yt/core/json/json_callbacks.cpp`.

| construct | JSON |
|---|---|
| node with attributes | `{"$attributes": {...}, "$value": <node>}` (`json_writer.cpp:359-381, 592-597`) |
| type annotation (`annotate_with_types`) | sibling key `"$type"`: `"int64"`, `"uint64"`, `"double"`, `"boolean"`, `"string"`, `"any"` (`json_writer.cpp:421,442,464,517,641`) |
| truncated value | `"$incomplete": true` (a real JSON boolean) (`json_writer.cpp:625-632`) |
| map key starting with `$` | prefixed with an extra `$` (`json_writer.cpp:571-576`; `IsSpecialJsonKey` at `YT/yt/core/json/helpers.cpp:7-10`) |
| entity `#` | JSON `null` (`json_writer.cpp:530-537`) |
| `stringify: true` | int64/uint64/double/boolean become **strings** (`json_writer.cpp:425,446,468,521`) |

`attributes_mode` defaults to `on_demand`: the `{$attributes,$value}` wrapper appears only when
the node actually has attributes. With `always` every node is wrapped; with `never` attributes
are dropped.

Client side, `HELP/lib/ypath/ypath.js:7-16` is the canonical reader:

```js
const VALUE_KEY = '$value';
const ATTRS_KEY = '$attributes';
const yson = {
    value(node)      { return node?.[VALUE_KEY] !== undefined ? node[VALUE_KEY] : node; },
    attributes(node) { return node?.[ATTRS_KEY] !== undefined ? node[ATTRS_KEY] : {}; },
};
```

Example — a `list` with `attributes: ["type","account"]` returned by v3, plain `json`:

```json
[
  {"$attributes": {"type": "map_node", "account": "tmp"}, "$value": "subdir"},
  {"$attributes": {"type": "table",    "account": "tmp"}, "$value": "my_table"}
]
```

The same with `TYPED_OUTPUT_FORMAT` (`stringify + annotate_with_types`), which is what the
navigation `execute_batch` requests:

```json
{
  "type":  {"$type": "string", "$value": "map_node"},
  "id":    {"$type": "string", "$value": "1-2-3-4"},
  "chunk_row_count": {"$type": "int64", "$value": "1000"},
  "dynamic": {"$type": "boolean", "$value": "false"}
}
```

### 4.2 v3 vs v4 envelope

`YT/yt/client/driver/command.cpp:41-60` implements the envelope difference:

| command | v3 body | v4 body |
|---|---|---|
| `get` | the value itself | `{"value": <value>}` |
| `list` | the list itself | `{"value": [...]}` |
| `exists` | `true` / `false` | `{"value": true}` |
| `execute_batch` | `[ {...}, {...} ]` | `{"results": [ {...}, {...} ]}` |
| `get_table_columnar_statistics` | a list (uses `ProduceOutput` directly, `table_commands.cpp:458`) | same list — **not** wrapped |
| empty-output commands (`set`, `remove`, …) | empty body | `{}` (`command.cpp:30-38`) |

The UI's TypeScript types mirror this exactly: `YTApiV3.get<Value>(...): Promise<Value>` vs
`YTApiV4.get<Value>(...): Promise<{value: Value}>` (`UI/src/ui/rum/rum-wrap-api.ts:55, 125`).

**The UI uses v3 for the navigation/table path except the table-meta
`get <path>/@has_row_level_ace` request, which uses v4.**

### 4.3 Error object schema

`YT/yt/core/misc/error.cpp:362-400` + `:313-341`:

```json
{
  "code": 500,
  "message": "Node //home/nope has no child with key \"nope\"",
  "attributes": {
    "pid": 1, "tid": 2, "thread": "Controller", "fid": 3,
    "host": "proxy-0", "datetime": "2026-07-25T10:00:00.000000Z",
    "trace_id": "...", "span_id": 0,
    "path": "//home/nope"
  },
  "inner_errors": [ { "code": 1, "message": "...", "attributes": {}, "inner_errors": [] } ]
}
```

`code` is an int64, `message` a string, `attributes` a flat map (the proxy adds `path` from the
request parameters — `context.cpp:850-859`), `inner_errors` a list of the same shape.
For a mock, `{"code": 500, "message": "...", "attributes": {}, "inner_errors": []}` is enough:
the UI only inspects `code`, `message`, `attributes` and `inner_errors`
(`UI/src/ui/common/yt-api.ts:44-64`, `UI/src/shared/utils/error.ts`).

Batch sub-request results keep the same shape under the `error` key:
`[{"error": {"code": 500, "message": "...", ...}}]`.

---

## 5. `web_json` — the table viewer's output format

The config struct is in `YT/yt/client/formats/config.h:363-381`, its parameters are
registered in `YT/yt/client/formats/config.cpp:321-339`, and the writer is implemented in
`YT/yt/library/formats/web_json_writer.cpp`.

### 5.1 Format attributes

| attribute | type | default | constraint |
|---|---|---|---|
| `max_selected_column_count` | int | `50` | ≥ 0 |
| `field_weight_limit` | int | `1024` (1 KiB) | ≥ 0 |
| `string_weight_limit` | int | `200` | ≥ 0 |
| `max_all_column_names_count` | int | `2000` | ≥ 0 |
| `column_names` | optional list\<string\> | unset | duplicates rejected (`web_json_writer.cpp:96-104`) |
| `value_format` | `schemaless` \| `yql` | `schemaless` | |

`skip_system_columns` exists as a C++ field (`config.h:373-374`) but is **deliberately not
registered** as a YSON parameter — it is always `true` from the wire's point of view. The
supported way to see a system column (`$row_index`, `$table_index`, …) is to name it in
`column_names` (`web_json_writer.cpp:657-661`).

### 5.2 Top-level response body

Emitted in this order (`web_json_writer.cpp:576-579, 741-777`):

```json
{
  "rows": [ {...}, {...} ],
  "incomplete_columns": "false",
  "incomplete_all_column_names": "false",
  "all_column_names": ["a", "b", "c"],
  "yql_type_registry": [ ... ]
}
```

* `rows` — list of maps, one per row. A column absent from a row is simply not a key.
* `incomplete_columns` / `incomplete_all_column_names` — **strings** `"true"`/`"false"`, not
  booleans (`web_json_writer.cpp:746-752`, with a `// TODO(levysotsky): Maybe we don't need
  stringification here?`).
* `all_column_names` — **sorted ascending** (`web_json_writer.cpp:757-762`). This is the list the
  UI uses to build its column set (`UI/src/ui/utils/navigation/content/table/table.js:33`).
* `yql_type_registry` — **only** when `value_format: "yql"` (`web_json_writer.cpp:771`;
  the schemaless writer's `WriteMetaInfo` is empty, `:485-486`).

Flag semantics (`web_json_writer.cpp:655-675`):

* `incomplete_all_column_names = "true"` ⟺ some distinct column could not be recorded because
  `all_column_names` had already reached `max_all_column_names_count`.
* `incomplete_columns = "true"` ⟺ some value was dropped by the column filter — either
  `max_selected_column_count` was reached, or `column_names` did not contain that column.
  Rejected columns still appear in `all_column_names`.

`column_names`, when present, **fully replaces** `max_selected_column_count` — the count limit is
not applied at all (`web_json_writer.cpp:49-89`). This is exactly why the UI's column-discovery
preload passes `column_names: []`: it wants `all_column_names` but zero row payload.

### 5.3 `value_format: schemaless` (default)

The writer feeds a JSON consumer with `Stringify = true, AnnotateWithTypes = true`
(`web_json_writer.cpp:418-421`), so the §4.1 conventions apply:

```json
{
  "rows": [
    {
      "id":       {"$type": "int64",   "$value": "-42"},
      "count":    {"$type": "uint64",  "$value": "100500"},
      "ratio":    {"$type": "double",  "$value": "3.14"},
      "ok":       {"$type": "boolean", "$value": "true"},
      "name":     {"$type": "string",  "$value": "row1_c"},
      "long_txt": {"$incomplete": true, "$type": "string", "$value": "rooooo"},
      "meta":     {"key": {"$type": "string", "$value": "a"}},
      "big_blob": {"$incomplete": true, "$type": "any", "$value": ""},
      "maybe":    null
    }
  ],
  "incomplete_columns": "false",
  "incomplete_all_column_names": "false",
  "all_column_names": ["big_blob","count","id","long_txt","maybe","meta","name","ok","ratio"]
}
```

Rules (`web_json_writer.cpp:438-483`, `YT/yt/core/json/json_writer.cpp:618-682`):

* int64/uint64/double/**boolean** are all **stringified** inside `$value`.
* `null` / entity is bare JSON `null`, with **no** `$type`/`$value` wrapper.
* only `string` and `any`/`composite` are weight-limited; numbers are never truncated.
* over-limit string ⇒ `$incomplete: true` + a raw **byte** prefix (not UTF-8 aware).
* over-limit composite/any ⇒ the whole value becomes
  `{"$incomplete": true, "$type": "any", "$value": ""}` (`json_writer.cpp:674-682`). The limit
  is compared against the value's **YSON byte length**, so small-looking structs can vanish at
  `field_weight_limit: 1024`.
* a fitting composite is expanded recursively; only the leaves carry `$type`.
* `string_weight_limit` is **ignored** in this mode.
* NaN/±Inf come out as ordinary strings `"nan"`/`"inf"`/`"-inf"` (because `Stringify` bypasses
  the infinity branch at `json_writer.cpp:473-503`).
* a column named `$foo` is emitted in `rows` as `"$$foo"`, but appears in `all_column_names`
  as `"$foo"` (`web_json_writer.cpp:724-728` vs `:766`).

### 5.4 `value_format: yql`

Every value becomes a **2-element array `[value, "<typeIndex>"]`** where the second element is the
**stringified** index into `yql_type_registry` (`web_json_writer.cpp:333-345`).

Value encoding (`YT/yt/library/formats/yql_yson_converter.cpp:80-260`):

```cpp
static constexpr TStringBuf KeyValue      = "val";
static constexpr TStringBuf KeyIncomplete = "inc";
static constexpr TStringBuf KeyBase64     = "b64";
```

* int64/uint64/double ⇒ **stringified**; boolean ⇒ **native JSON boolean** (the one asymmetry
  vs schemaless).
* optional of a non-nullable element ⇒ wrapped in a 1-element list `[v]`; `null` stays `null`.
* list/dict ⇒ always a map `{"val": [...]}`.
* struct/tuple/variant ⇒ a plain JSON list.
* non-UTF-8 string ⇒ `{"b64": true, "val": "<base64>"}`.
* truncated (string, list, or whole subtree) ⇒ `{"inc": true, "val": …}`; `inc` and `b64` can
  co-occur, at **any nesting depth**.
* `any`/`Yson` columns ⇒ `{"val": <schemaless-style JSON with $type/$value>}`.

Registry entries are tag-first JSON lists (`web_json_writer.cpp:181-288`):

```
["DataType", "Int64"]                              ["NullType"]   ["VoidType"]
["DataType", "Decimal", "<precision>", "<scale>"]
["OptionalType", <t>]     ["ListType", <t>]        ["DictType", <k>, <v>]
["StructType", [["name", <t>], ...]]               ["TupleType", [<t>, ...]]
["VariantType", ["StructType", [...]]]             ["VariantType", ["TupleType", [...]]]
["TaggedType", "<tag>", <t>]
```

Simple names come from `GetSimpleYqlTypeName` (`web_json_writer.cpp:110-179`); notably
`Any → "Yson"`.

Example:

```json
{
  "rows": [{
    "int64_column":  [["-42"], "0"],
    "string_column": [["abcdefghij"], "1"],
    "bad_utf8":      [[{"val": "//79/A==", "b64": true}], "1"],
    "list_column":   [[{"val": ["11","12","13"], "inc": true}], "2"],
    "yson_column":   [[{"val": {"$type": "string", "$value": "just a string"}}], "3"]
  }],
  "incomplete_columns": "false",
  "incomplete_all_column_names": "false",
  "all_column_names": ["bad_utf8","int64_column","list_column","string_column","yson_column"],
  "yql_type_registry": [
    ["OptionalType", ["DataType", "Int64"]],
    ["OptionalType", ["DataType", "String"]],
    ["OptionalType", ["ListType", ["DataType", "Int64"]]],
    ["OptionalType", ["DataType", "Yson"]]
  ]
}
```

Caveat for anyone writing a *faithful* mock: in yql mode `field_weight_limit` is a **running
byte budget** — `totalLimit = bytesWrittenSoFar + FieldWeightLimit`
(`yql_yson_converter.cpp:840-845`) — so truncation depends on a value's position in the row.
A mock does not need to reproduce this.

### 5.5 How the UI consumes it

`UI/src/ui/utils/navigation/content/table/table.js:28-36`:

```js
export function prepareRows(rowData, reverseRows = false) {
    const data = JSON.parse(rowData);
    const rows = reverseRows ? reverse_(data.rows) : data.rows;
    return {rows, columns: data.all_column_names, yqlTypes: data.yql_type_registry || null};
}
```

`UI/src/ui/utils/navigation/content/table/table.js:44-58` reads
`x-yt-response-parameters` → `omitted_inaccessible_columns`.
`useYqlTypes` is derived from the request's `output_format/@value_format === 'yql'`
(`UI/src/ui/store/actions/navigation/content/table/readStaticTable.ts:36-43`).

**Minimum a mock must return for the table to render:** a JSON body with `rows` and
`all_column_names`. `incomplete_columns` / `incomplete_all_column_names` /
`yql_type_registry` are optional as far as the UI code path is concerned.

---

## 6. Exact call sequences

### 6.1 App bootstrap / cluster selection

Cluster configs are **embedded in the served HTML** as `window.YT` and `window.__DATA__`
(`UI/src/server/components/layout-config.ts:56-75`), not fetched.

Only on the root page `/`:

1. `GET /api/clusters/versions` — `UI/src/ui/store/actions/clusters-menu.ts:27-41`
2. `GET /api/clusters/auth-status` — `UI/src/ui/store/actions/clusters-menu.ts:43-57`

On mounting a cluster page (`updateCluster`, `UI/src/ui/store/actions/cluster-params.ts:197-280`):

3. `GET /api/cluster-info/<cluster>` (axios → UI Node). Server does
   `GET <proxy>/auth/whoami` and `GET <proxy>/version`
   (`UI/src/server/components/cluster-queries.ts:52-81, 122-155`). Response:
   ```json
   {"token": {"login": "root", "csrf_token": "abcdef"}, "version": "24.1.0-mock"}
   ```
   Missing/failed `version` blocks the whole app (`PRELOAD_ERROR.CONNECTION`); missing
   `csrf_token` blocks it with `PRELOAD_ERROR.AUTHENTICATION`
   (`cluster-params.ts:235-257`).
4. `initYTApiClusterParams(cluster)` — no I/O.
5. `POST /api/v3/execute_batch` with one `check_permission_by_acl` sub-request (is the user an
   admin?) — `UI/src/shared/utils/check-permission.ts:8-57`:
   ```json
   {"requests":[{"command":"check_permission_by_acl","parameters":{"acl":[{"permissions":["write"],"subjects":["admins"],"action":"allow"}],"user":"root","permission":"write"}}]}
   ```
   Failure is swallowed (`checkIsDeveloper` catches and returns `false`), so a mock may answer
   `[{"error": {...}}]`.
6. `GET /api/cluster-params/<cluster>` (axios → UI Node). Server does two `execute_batch`
   round-trips (`UI/src/server/components/cluster-params.ts:60-201`):
   * batch #1: `list //sys/primary_masters`
   * batch #2: `list //sys/media`, `get //sys/scheduler/orchid/service/version`,
     `get //sys/@ui_config`, `get //sys/@ui_config_dev_overrides`,
     `get //sys/primary_masters/<first>/orchid/service/version`

   All sub-requests carry `suppress_transaction_coordinator_sync: true` and
   `suppress_upstream_sync: true` (`UI/src/shared/constants/index.ts:14-17`).
   `NODE_DOES_NOT_EXIST` (code 500) on `@ui_config`, `@ui_config_dev_overrides`, or either
   version path is tolerated (`cluster-params.ts:203-230`). Only a `mediumList` error is fatal
   client-side (`UI/src/ui/store/actions/cluster-params.ts:101-104`).
   Response shape the client expects:
   ```json
   {"mediumList": {"output": ["default"]},
    "schedulerVersion": {"output": "24.1.0-mock"},
    "masterVersion": {"output": "24.1.0-mock"},
    "uiConfig": {"output": {}},
    "uiDevConfig": {"output": {}}}
   ```
7. `POST /api/settings/<cluster>/<user>/` then `GET /api/settings/<cluster>/<user>/`
   (axios → UI Node) — `UI/src/ui/common/utils/settings-remote-provider.ts:17-71`,
   `UI/src/ui/store/actions/settings/index.ts:114-135`. Errors are swallowed; a
   `NODE_DOES_NOT_EXIST` body yields `undefined` (`settings-remote-provider.ts:39-41`).
   These only fire when `userSettingsConfig` is configured server-side
   (`UI/src/server/components/settings.ts:27-30`).
8. `GET /api/v3/get_supported_features` — `UI/src/ui/store/actions/global/supported-features.ts:29-30`,
   re-fired every 600 s. Response `{"features": {...}}`; failure is non-fatal.

### 6.2 Navigating to a Cypress path

`updateView` — `UI/src/ui/store/actions/navigation/index.ts:53-207`, triggered from
`UI/src/ui/pages/navigation/Navigation/Navigation.js:122-140`.

**Main request** (`index.ts:75-94`) — `POST /api/v3/execute_batch`, api-id `navigationAttributes`:

```json
{
  "requests": [{"command": "get",
                "parameters": {"path": "<path>/@", "attributes": [ ...see below... ]}}],
  "output_format": {"$value": "json", "$attributes": {"stringify": true, "annotate_with_types": true}}
}
```

`transaction_id` is added when a transaction is selected
(`UI/src/ui/utils/navigation/index.ts:42-65`).

The attribute list, **verbatim** from `UI/src/ui/store/actions/navigation/index.ts:274-346`:

```
_format, _read_schema, _restore_path, _yql_key_meta, _yql_op_id, _yql_runner, _yql_row_spec,
_yql_subkey_meta, _yql_type, _yql_value_meta, access_time, account, acl, atomicity, broken,
chunk_count, chunk_row_count, cluster_name, compressed_data_size, compression_codec,
compression_ratio, creation_time, data_weight, default_disk_space, disk_space, dynamic,
effective_expiration, enable_dynamic_store_read, erasure_codec, expiration_time,
expiration_timeout, id, in_memory_mode, key, key_columns, leader_controller_address, lock_count,
lock_mode, locks, mode, modification_time, monitoring_cluster, monitoring_project, optimize_for,
owner, path, pipeline_format_version, primary_medium, queue_static_export_destination,
remount_needed_tablet_count, replica_path, replicated_table_options, replication_factor,
resource_usage, schema, schema_mode, security_tags, sorted, sorted_by, start_time, state,
tablet_cell_bundle, tablet_count, tablet_error_count, tablet_state, target_path, timeout, title,
treat_as_queue_consumer, type, uncompressed_data_size
```

(plus whatever `UIFactory.getNavigationExtraTabs()` contributes, `index.ts:348-358`).

Note **`row_count` is not requested** — the UI derives the row count from `chunk_row_count`.
`schema` from this single request is what feeds the table's schema tab and column typing;
there is **no separate `get //path/@schema` call**.

**Fired in parallel from the same thunk:**

| purpose | request | ref |
|---|---|---|
| tablet-error polling gate | `execute_batch [get <path>/@type, get <path>/@dynamic]` | `UI/src/ui/store/actions/navigation/tabs/tablet-errors/tablet-errors-background.ts:60-80` |
| mount config | `execute_batch [get <path>/@mount_config]` (tolerates code 500) | `UI/src/ui/store/actions/navigation/content/table/table-mount-config.ts:13-39` |
| description tab | `execute_batch [get <path>/@ attributes=[annotation, annotation_path]]` | `UI/src/ui/store/api/navigation/tabs/description.ts:24-50` |
| permissions (only when `account` is set) | `execute_batch [check_permission{user, write, path}, check_permission{user, use, //sys/accounts/<account>}]` | `index.ts:155-171`, `UI/src/ui/utils/acl/acl-api.ts:209-224` |
| queue-origin lookup | `execute_batch [get #<queueId>/@path]`; dispatched unconditionally, so a missing destination attribute produces `#undefined/@path` and an ignored per-item error | `UI/src/ui/store/actions/navigation/tabs/queue/exports.ts:11-36` |

**On failure only** (`index.ts:192-193`): `POST /api/v3/exists {"path": "//sys/idm/lock"}`.

### 6.3 Listing a `map_node`

`fetchNodes` — `UI/src/ui/store/actions/navigation/content/map-node.js:47-86`, a plain
`POST /api/v3/list` (not a batch), api-id `navigationListNodes`:

```json
{"path": "<path>", "attributes": [
  "type","dynamic","row_count","unmerged_row_count","chunk_row_count","modification_time",
  "creation_time","resource_usage","sorted","data_weight","account","target_path","broken",
  "lock_count","tablet_state","_restore_path","expiration_time","expiration_timeout",
  "effective_expiration","treat_as_queue_consumer","treat_as_queue_producer","path",
  "pipeline_format_version"
], "suppress_access_tracking": "true"}
```

(`row_count` and `unmerged_row_count` carry `// Deprecated` comments at
`map-node.js:57-58` but are still sent.)

Response is unwrapped with `ypath.getValue` (`map-node.js:97`), so a v3 bare list is expected:

```json
[
  {"$attributes": {"type":"map_node","account":"tmp","modification_time":"2026-07-25T10:00:00.000000Z"}, "$value": "subdir"},
  {"$attributes": {"type":"table","account":"tmp","dynamic":false,"sorted":false,"chunk_row_count":1000,"data_weight":54321,"resource_usage":{"disk_space":12345}}, "$value": "my_table"}
]
```

On demand (the "recursive usage" toggle), chunks of ≤ 200 `get` sub-requests in
`execute_batch`, each `{"path": "<node>&/@recursive_resource_usage", "timeout": 60000}` —
note the `&` link-suppression suffix (`map-node.js:136-240`).

### 6.4 Opening a static table

`getTableData` — `UI/src/ui/store/actions/navigation/content/table/table.js:471-550`, triggered
from `UI/src/ui/pages/navigation/content/Table/Table.js:170-177`.

**Step 0 (optional)** — if the URL carries `columns=<hash>`:
`GET /api/table-column-preset/<cluster>/<hash>` (UI Node)
— `UI/src/ui/store/actions/navigation/content/table/columns-preset.ts:53-58`.

**Step 1 — column-discovery preload** (`restoreColumns`, `table.js:361-393`):
`POST /api/v3/read_table` with

```json
{
  "path": "<path>[#0:#0]",
  "table_reader": {"workload_descriptor": {"category": "user_interactive"}},
  "output_format": {"$value": "web_json", "$attributes": {
      "field_weight_limit": <cellSize>,
      "max_selected_column_count": 50,
      "max_all_column_names_count": 3000,
      "column_names": []}},
  "suppress_access_tracking": "true",
  "dump_error_into_response": true,
  "omit_inaccessible_columns": true,
  "omit_inaccessible_rows": true
}
```

The range is `[#0:#0]` when the schema is strict, otherwise
`[#<offset>:#<offset+pageSize+1>]` (`table.js:319-321, 342`). `column_names: []` means "return
no cell values but still report `all_column_names`".

**Step 2 — the real page** (`loadStaticTable`, `table.js:288-337` → `updateTableData`,
`table.js:395-469`): same shape, with

```json
"path": "<path>[#0:#51]",
"output_format": {"$value": "web_json", "$attributes": {
    "field_weight_limit": <cellSize>,
    "string_weight_limit": <round(cellSize/10)>,
    "max_selected_column_count": <user setting>,
    "max_all_column_names_count": 3000,
    "value_format": "yql",            // only when the YQL-types setting is on
    "column_names": ["a", "b"]        // only when some columns are checked/key
}}
```

Notes:

* **The row range is a YPath string suffix**, `path[#lower:#upper]`, *not* a
  `ranges/lower_limit/upper_limit` object. The object form is used only for the
  unmounted-unsorted **dynamic** table fallback (`table.js:132-162`).
* `requestedPageSize = pageSize + 1` — the UI fetches one extra row to detect end-of-table
  (`UI/src/ui/store/selectors/navigation/content/table.js:77`).
* Fixed extras always present: `dump_error_into_response: true`,
  `omit_inaccessible_columns: true`, `omit_inaccessible_rows: true`
  (`UI/src/ui/store/actions/navigation/content/table/readTable.ts:21-25`).
* The wrapper's `readTable` sets `transformResponse` to `{data, headers}` so the UI can read
  `x-yt-response-parameters` (`readTable.ts:6-19`).

**Step 3 — table meta** (`TableMeta`, `UI/src/ui/pages/navigation/content/Table/TableMeta/TableMeta.tsx:43-93`):
rendered **entirely from the `updateView` attributes** (`schema`, `chunk_count`,
`chunk_row_count`, `dynamic`, `sorted`, `compressed_data_size`, …). The only extra requests:
`check_permission {path, permission: 'full_read', user}` (v3) and
`get {path: "<path>/@has_row_level_ace"}` (v4) —
`UI/src/ui/pages/navigation/content/Table/table-hooks/useTableAccessMetaItem.tsx:21-34`.
Both are optional for a mock (failures degrade gracefully).

**Cell preview** (clicking a cell) reuses `readStaticTable` with
`path = <path>{"<column>"}[#<row>:#<row+1>]`
(`UI/src/ui/store/actions/navigation/modals/cell-preview/static-table.ts:6-22`).

**Download** builds a direct URL rather than an XHR:
`/api/yt/<cluster>/api/v3/read_table` or `//<externalProxy ?? proxy>/api/v3/read_table`
depending on `allowDirectDownload()` (`UI/src/ui/utils/navigation/index.ts:148-166`).

---

## 7. Minimal call list for a mock backend

Target: default UI configuration (`ytApiUseCORS` falsy), so the mock can be **one HTTP origin**
that plays both the UI Node server's REST endpoints *and* the reverse-proxied YT API — or it can
be a fake **cluster proxy** sitting behind the real UI Node server (the `proxy` field of the
cluster config). The second option is less work: only the right-hand column below is needed.

### 7.1 Must implement (cluster-proxy surface)

| # | Request | Minimal response |
|---|---|---|
| 1 | `GET /version` | `24.1.0-mock` (plain text) |
| 2 | `GET /auth/whoami` | `{"login":"root","csrf_token":"mock-csrf"}` |
| 3 | `GET /hosts` | `["<mock-host>"]` — only needed if the UI Node's heavy-proxy path is active; return the mock's own host |
| 4 | `POST /api/v3/execute_batch` | `[{"output": ...}, ...]`, one element per sub-request, dispatching on `command` ∈ {`get`, `list`, `exists`, `check_permission`, `check_permission_by_acl`} |
| 5 | `POST /api/v3/get` | the value (v3 = unwrapped). Must honour `path` with `/@` and `/@<attr>` suffixes and the `attributes` filter |
| 6 | `POST /api/v3/list` | list of child keys; when `attributes` is non-empty, entries are `{"$attributes":{...},"$value":"name"}` |
| 7 | `POST /api/v3/exists` | `true` / `false` |
| 8 | `POST /api/v3/read_table` | `web_json` body (§5.2) + `X-YT-Response-Parameters` |
| 9 | `GET /api/v3/get_supported_features` | `{"features": {}}` — failure is non-fatal, so this is optional |

Paths that must resolve for the bootstrap batches (§6.1 step 6):
`//sys/primary_masters` (list), `//sys/media` (list),
`//sys/scheduler/orchid/service/version` (get),
`//sys/@ui_config` (get), `//sys/@ui_config_dev_overrides` (get),
`//sys/primary_masters/<name>/orchid/service/version` (get).
The final four may legitimately answer with error code **500** (`NODE_DOES_NOT_EXIST`);
the primary-master version request is omitted when `//sys/primary_masters` is empty.

Per-response headers worth emitting on every YT API response:

```
Content-Type: application/json
X-YT-Proxy: mock-proxy-0
X-YT-Request-Id: <uuid>
Trailer: X-YT-Error, X-YT-Response-Code, X-YT-Response-Message
```

and, on error, `X-YT-Error` / `X-YT-Response-Code` / `X-YT-Response-Message` with HTTP **400**.

### 7.2 Must implement if the mock also replaces the UI Node server

| Request | Minimal response |
|---|---|
| `GET /api/cluster-info/:cluster` | `{"token":{"login":"root","csrf_token":"mock-csrf"},"version":"24.1.0-mock"}` |
| `GET /api/cluster-params/:cluster` | `{"mediumList":{"output":["default"]},"schedulerVersion":{"output":"24.1.0-mock"},"masterVersion":{"output":"24.1.0-mock"},"uiConfig":{"output":{}},"uiDevConfig":{"output":{}}}` |
| `GET /api/clusters/versions` | `[{"id":"mock","version":"24.1.0"}]` (root page only) |
| `GET /api/clusters/auth-status` | `{"mock":{"authorized":true}}` (root page only) |
| `GET|POST /api/settings/:cluster/:user`; `GET|PUT|DELETE /api/settings/:cluster/:user/:path` | `{}` / 200 — only if `userSettingsConfig` is configured |
| `POST /api/yt/:cluster/login` | proxies to `/login`; only if password auth is enabled |
| `GET|POST|PUT /api/yt/:cluster/api/:version/:command` | forward to §7.1 |

### 7.3 Can be skipped entirely

* Framing (`X-YT-Accept-Framing`) — the UI never negotiates it.
* Heavy-proxy redirects (307) — the UI always sends `X-YT-Suppress-Redirect: 1`.
* `get_table_columnar_statistics` — no call site in the UI.
* CORS — irrelevant same-origin; add the §3.8 headers only if `ytApiUseCORS` is turned on.
* v4 — the whole navigation + static-table path uses v3. (`get /@has_row_level_ace` is the one
  v4 call, and it degrades gracefully.)
* `select_rows`, `check_permission` with `columns`, mount/unmount — dynamic-table only.
* ETag / `If-None-Match` — the UI does not send it on these paths.

### 7.4 Parameter-parsing checklist for the mock's request decoder

For each `/api/vN/<command>` request, build the parameter map by merging, in order:

1. `{}` (or the inferred formats, if you care);
2. the query string (rarely used by the UI, but the Node server does forward `req.query`);
3. `X-YT-Parameters` verbatim, **or** the base64 concatenation of
   `X-YT-Parameters0/1/…` / `X-YT-Parameters-0/-1/…`;
4. the JSON POST body (this is where `get`/`list`/`exists`/`execute_batch`/`read_table`
   parameters actually live for the UI).

Then handle these path forms: `//p`, `//p/@`, `//p/@attr`, `//p&/@attr` (link suppression),
`#<object-id>/@attr`, `//p[#a:#b]` (row range), `//p{"col"}[#a:#b]` (column + row range),
and the object form `{"$value": "//p", "$attributes": {"ranges": [...]}}`.

---

## 8. Quick reference: header cheat-sheet

Request (browser → UI Node → cluster proxy):

```
Accept: application/json
Content-Type: application/json                 (POST/PUT)
X-YT-Suppress-Redirect: 1
X-YT-Header-Format: <encode_utf8=%false>json   (only with X-YT-Parameters-N)
X-YT-Parameters-0: <base64 json>               (only when params are not in the body)
X-YT-Input-Format / X-YT-Output-Format         (not used by the UI; params carry the formats)
X-Csrf-Token: <ytfront_<cluster>_xsrf_token>   (only when authentication != 'none')
X-Custom-Request-Id: <YTApiId>                 (UI-only diagnostic, stripped by the Node server)
X-YT-Correlation-Id: <req id>                  (added by the UI Node server)
Cookie: YTCypressCookie=<secret> | access_token=<oauth token>
                                                 (added by the UI Node server only when authentication != 'none')
```

Response (cluster proxy → browser):

```
Content-Type: application/json
X-YT-Proxy: <host>
X-YT-Request-Id: <uuid>
X-YT-Trace-Id: <uuid>
Trailer: X-YT-Error, X-YT-Response-Code, X-YT-Response-Message
X-YT-Response-Parameters: {"approximate_row_count":…,"omitted_inaccessible_columns":[],"start_row_index":…}
X-YT-Framing: 1                                (only if framing negotiated)
--- on error ---
X-YT-Error: {"code":500,"message":"…","attributes":{},"inner_errors":[]}
X-YT-Error-Content-Type: application/json
X-YT-Response-Code: 500
X-YT-Response-Message: …
```
