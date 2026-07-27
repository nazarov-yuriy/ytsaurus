# Play-session coverage vs documented API catalog

Recorded: 165 proxy-side requests, 119 browser-side requests, 61 distinct shapes (see corpus.json).

## Distinct request shapes recorded

- `[browser] GET /api/access-log/mock/check-available` ×6 → [200]
- `[browser] GET /api/cluster-info/mock` ×6 → [200]
- `[browser] GET /api/cluster-params/mock` ×6 → [200]
- `[browser] GET /api/clusters/auth-status` ×1 → [200]
- `[browser] GET /api/clusters/versions` ×1 → [200]
- `[browser] GET /api/yt-proxy/mock/hosts-all` ×1 → [200]
- `[browser] GET /api/yt/mock/api/v3/get_supported_features` ×6 → [200]
- `[browser] POST /api/yt/logout` ×1 → [404]
- `[browser] POST /api/yt/mock/api/v3/check_permission params(permission,user)` ×3 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[]` ×1 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[check_permission]` ×8 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[check_permission_by_acl]` ×6 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[get:@(all)]` ×14 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[get:@acl,get:@effective_acl,get:@revision]` ×1 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[get:@dynamic,get:@type]` ×8 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[get:@mount_config]` ×7 → [200]
- `[browser] POST /api/yt/mock/api/v3/execute_batch batch[get:@path]` ×7 → [200]
- `[browser] POST /api/yt/mock/api/v3/exists params(suppress_access_tracking)` ×7 → [200]
- `[browser] POST /api/yt/mock/api/v3/get @(all) params(output_format,suppress_access_tracking) of=json` ×2 → [200]
- `[browser] POST /api/yt/mock/api/v3/get @default_tree params(suppress_access_tracking)` ×6 → [200]
- `[browser] POST /api/yt/mock/api/v3/get @inherit_acl params(suppress_access_tracking)` ×1 → [200]
- `[browser] POST /api/yt/mock/api/v3/get @opaque_attribute_keys params(suppress_access_tracking)` ×2 → [200]
- `[browser] POST /api/yt/mock/api/v3/get @path params(suppress_access_tracking)` ×1 → [200]
- `[browser] POST /api/yt/mock/api/v3/get @user_attributes params(output_format,suppress_access_tracking) of=json` ×1 → [200]
- `[browser] POST /api/yt/mock/api/v3/list params(attributes,suppress_access_tracking)` ×3 → [200]
- `[browser] POST /api/yt/mock/api/v3/read_table params(dump_error_into_response,omit_inaccessible_columns,omit_inaccessible_rows,output_format,suppress_access_tracking,table_reader) of=web_json` ×9 → [200]
- `[browser] POST /api/yt/mock/api/v4/get @has_row_level_ace` ×3 → [200]
- `[browser] POST /api/yt/mock/login params(password,username)` ×1 → [200]
- `[proxy] GET /api` ×1 → [200]
- `[proxy] GET /api/v3/get_supported_features` ×6 → [200]
- `[proxy] GET /auth/whoami` ×6 → [200]
- `[proxy] GET /hosts` ×1 → [200]
- `[proxy] GET /hosts/all` ×2 → [200]
- `[proxy] GET /ping` ×1 → [200]
- `[proxy] GET /version` ×7 → [200]
- `[proxy] POST /api/v3/check_permission params(permission,user)` ×3 → [200]
- `[proxy] POST /api/v3/execute_batch batch[]` ×1 → [200]
- `[proxy] POST /api/v3/execute_batch batch[check_permission]` ×8 → [200]
- `[proxy] POST /api/v3/execute_batch batch[check_permission_by_acl]` ×6 → [200]
- `[proxy] POST /api/v3/execute_batch batch[get,get:@ui_config,get:@ui_config_dev_overrides,list]` ×23 → [200]
- `[proxy] POST /api/v3/execute_batch batch[get:@(all)]` ×14 → [200]
- `[proxy] POST /api/v3/execute_batch batch[get:@acl,get:@effective_acl,get:@revision]` ×1 → [200]
- `[proxy] POST /api/v3/execute_batch batch[get:@dynamic,get:@type]` ×8 → [200]
- `[proxy] POST /api/v3/execute_batch batch[get:@mount_config]` ×7 → [200]
- `[proxy] POST /api/v3/execute_batch batch[get:@path]` ×7 → [200]
- `[proxy] POST /api/v3/execute_batch batch[list]` ×23 → [200]
- `[proxy] POST /api/v3/exists params(suppress_access_tracking)` ×7 → [200]
- `[proxy] POST /api/v3/get @(all) params(output_format,suppress_access_tracking) of=json` ×2 → [200]
- `[proxy] POST /api/v3/get @default_tree params(suppress_access_tracking)` ×6 → [200]
- `[proxy] POST /api/v3/get @inherit_acl params(suppress_access_tracking)` ×1 → [200]
- `[proxy] POST /api/v3/get @opaque_attribute_keys params(suppress_access_tracking)` ×2 → [200]
- `[proxy] POST /api/v3/get @path params(suppress_access_tracking)` ×1 → [200]
- `[proxy] POST /api/v3/get @user_attributes` ×1 → [200]
- `[proxy] POST /api/v3/get @user_attributes params(output_format,suppress_access_tracking) of=json` ×1 → [200]
- `[proxy] POST /api/v3/get_table_columnar_statistics params(paths)` ×1 → [200]
- `[proxy] POST /api/v3/list params(attributes,suppress_access_tracking)` ×3 → [200]
- `[proxy] POST /api/v3/read_table params(dump_error_into_response,omit_inaccessible_columns,omit_inaccessible_rows,output_format,suppress_access_tracking,table_reader) of=web_json` ×9 → [200]
- `[proxy] POST /api/v4/exists` ×1 → [200]
- `[proxy] POST /api/v4/get @has_row_level_ace` ×3 → [200]
- `[proxy] POST /api/v4/list` ×1 → [200]
- `[proxy] POST /login` ×1 → [200]

## Proxy endpoints: documented but NOT exercised

Mock-critical (1):
- `/ready` ()

Out-of-scope/optional (16): `/api/v3`, `/api/v3/check_permission_by_acl`, `/api/v3/list_operations`, `/api/v3/whoami`, `/api/v4`, `/api/v4/discover_proxies`, `/api/v4/get_current_user`, `/api/v4/get_query_tracker_info`, `/api/v4/issue_token`, `/api/v4/list_user_tokens`, `/api/v4/revoke_token`, `/api/v4/set_user_password`, `/cluster_connection`, `/internal/discover_versions/v2`, `/service`, `/service/version`

## Proxy endpoints: exercised (20)

- `/api`
- `/api/:version/:command`
- `/api/v3/*`
- `/api/v3/check_permission`
- `/api/v3/execute_batch`
- `/api/v3/exists`
- `/api/v3/get`
- `/api/v3/get_supported_features`
- `/api/v3/get_table_columnar_statistics`
- `/api/v3/list`
- `/api/v3/read_table`
- `/api/v4/exists`
- `/api/v4/get`
- `/api/v4/list`
- `/auth/whoami`
- `/hosts`
- `/hosts/all`
- `/login`
- `/ping`
- `/version`

## UI-server endpoints: documented but NOT exercised

Mock-critical (6):
- `/` ()
- `/:ytAuthCluster/` ()
- `/:ytAuthCluster/*` ()
- `/:ytAuthCluster/:page` ()
- `/:ytAuthCluster/:page/:operation/:job/:tab` ()
- `/:ytAuthCluster/change-password/` ()

Out-of-scope/optional (28): `/api/:ytAuthCluster/prometheus/chart-data`, `/api/:ytAuthCluster/prometheus/discover-values`, `/api/accounts-usage/:ytAuthCluster/:action`, `/api/accounts-usage/:ytAuthCluster/check-available`, `/api/code-assistant/*`, `/api/markdown-to-html`, `/api/oauth/callback`, `/api/oauth/logout/callback`, `/api/odin/clusters/availability`, `/api/odin/proxy/:action/:ytAuthCluster?`, `/api/pool-names/:ytAuthCluster`, `/api/remote-copy`, `/api/settings/:ytAuthCluster/:username`, `/api/settings/:ytAuthCluster/:username/:path`, `/api/settings/:ytAuthCluster/:username[/:path]`, `/api/strawberry/:engine/:ytAuthCluster/:action`, `/api/table-column-preset/:ytAuthCluster/:hash`, `/api/table-column-preset/:ytAuthCluster[/:hash]`, `/api/tablet-errors/:ytAuthCluster/:action`, `/api/vcs`, `/api/vcs/branches`, `/api/vcs/file`, `/api/vcs/repositories`, `/api/vcs/token`, `/api/vcs/tokens-availability`, `/api/yt/:ytAuthCluster/change-password`, `/oauth/login`, `/ping`

## UI-server endpoints: exercised (14)

- `/:ytAuthCluster/:page/:computations/:computation/:partition/:id`
- `/:ytAuthCluster/:page/:operation/:tab`
- `/:ytAuthCluster/:page/:tab`
- `/:ytAuthCluster/:page?/:tab?`
- `/api/access-log/:ytAuthCluster/:action`
- `/api/access-log/:ytAuthCluster/check-available`
- `/api/cluster-info/:ytAuthCluster`
- `/api/cluster-params/:ytAuthCluster`
- `/api/clusters/auth-status`
- `/api/clusters/versions`
- `/api/yt-proxy/:ytAuthCluster/:command`
- `/api/yt/:ytAuthCluster/api/:version/:command`
- `/api/yt/:ytAuthCluster/login`
- `/api/yt/logout`

## Notes

- HTML page routes (`/`, `/:ytAuthCluster/...`) listed as unexercised were in fact loaded by the play session; the HAR filter only keeps `/api/*` requests, so they never enter the hit set. Treat them as covered by any page navigation.
- `/ready` is a deployment readiness endpoint, not UI traffic; Helm probes and backend tests exercise it outside this recorded play session.
- `POST /api/yt/logout` returns 404 because the logout route is only mounted when the UI server's auth policy is enabled; with `authentication: "none"` there is no session to destroy.
- `POST /api/yt/mock/login` succeeds even in auth-none mode: the UI server forwards Basic auth to the proxy `/login`, which sets `YTCypressCookie`.
- Batch-level errors (e.g. nonexistent paths) travel inside HTTP-200 `execute_batch` responses as per-item `{error}` objects — HTTP status stays 200.
