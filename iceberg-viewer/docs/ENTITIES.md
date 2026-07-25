# Entities (generated from db/api_catalog.sqlite — do not edit by hand)

Every payload entity and every field, with mock support status: 🟢 implemented (dynamic, reimplement over Iceberg), 🟡 constant (stubbed, keep as-is), ⚪ unused (documented, not needed by the viewer).

## cluster-info-response

Body of GET /api/cluster-info/:cluster (UI server; fans out to proxy /auth/whoami + /version). A truthy version and token.csrf_token are required for a successful mount, but failed upstream calls omit token/version and populate the corresponding error field.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `token` | whoami-response |  | proxied /auth/whoami body; required for a successful mount |
| 🟡 constant | `version` | string |  | raw proxy /version text ('mock-proxy-1.0.0' in the mock); bootstrap checks only that it is truthy |
| ⚪ unused | `tokenError` | {message, code?, inner_errors} |  | set when whoami fails -> PRELOAD_ERROR.AUTHENTICATION |
| ⚪ unused | `versionError` | {message, code?, inner_errors} |  | set when version fails -> PRELOAD_ERROR.CONNECTION |

## cluster-params-response

Body of GET /api/cluster-params/:cluster (UI server; two execute_batch calls against the proxy). Each field is a batch item {output?} | {error?}.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `mediumList` | batch-item<list<string>> | yes | list //sys/media; a present error aborts cluster init — MUST succeed |
| 🟡 constant | `masterVersion` | batch-item<string or list<string>> | yes | version string from the first primary master; when the primary-master list is empty, the list batch item is returned instead (output: [] in the mock) |
| 🟡 constant | `schedulerVersion` | batch-item<string> | yes | get //sys/scheduler/orchid/service/version; mock 500 -> UI falls back to '0.0.0-unknown' |
| 🟡 constant | `uiConfig` | batch-item<map> | yes | get //sys/@ui_config; code-500 error tolerated |
| 🟡 constant | `uiDevConfig` | batch-item<map> | yes | get //sys/@ui_config_dev_overrides; code-500 error tolerated |

## clusters-config-entry

One cluster in the UI server's clusters-config.json (server-rendered into window.YT.clusters; no /clusters endpoint exists).

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `authentication` | string |  | 'none', 'basic', or 'domain'; omitted values default to 'none', and the UI otherwise only tests !== 'none' |
| 🟢 implemented | `disableHeavyProxies` | bool |  | true -> UI server never calls /hosts for heavy commands |
| 🟢 implemented | `id` | string | yes | URL segment (/:cluster/navigation) |
| 🟢 implemented | `name` | string | yes | display name |
| 🟢 implemented | `proxy` | string | yes | host:port of the HTTP proxy — points at the mock |
| 🟢 implemented | `secure` | bool |  | false -> http scheme to proxy |
| 🟡 constant | `description` | string |  | tooltip text |
| 🟡 constant | `environment` | string | yes | badge: development/production/... |
| 🟡 constant | `group` | string |  | cluster list grouping |
| 🟡 constant | `theme` | string | yes | cluster color theme |
| ⚪ unused | `primaryMaster` | map |  | cellTag etc.; not needed by the viewer |

## execute-batch-request

Body of POST /api/v3/execute_batch.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `output_format` | format |  | governs the OUTER envelope; annotate_with_types wraps every scalar as {$type,$value} |
| 🟢 implemented | `requests` | list<{command, parameters}> | yes | sub-commands dispatched against the same command table |

## execute-batch-result-item

One element of the execute_batch response list, in request order.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `error` | yt-error |  | per-item failure; HTTP status stays 200 |
| 🟢 implemented | `output` | yson |  | sub-command result on success |

## login-request

Password login: browser POSTs JSON to UI server /api/yt/:cluster/login; UI server converts to HTTP Basic and calls proxy POST /login; empty 200 + Set-Cookie YTCypressCookie (no SameSite), duplicated as <cluster>_YTCypressCookie by the UI server.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `password` | string | yes | plain password; a wrong pair returns HTTP 401 with generic code 1 ('Incorrect login or password'), matching the real proxy which masks the cause |
| 🟢 implemented | `username` | string | yes | checked against users map in data.js |

## node-attributes

Cypress node attributes: union of what the UI requests (navigation ~70-attribute get <path>/@ batch, tabs, probes) and what the mock provides. Virtual attributes included. Required marks data the Iceberg-backed implementation must provide on the applicable node kind; table-only fields are absent on map nodes.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `chunk_count` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `chunk_row_count` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `compressed_data_size` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `data_weight` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `dynamic` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `id` | yson | yes | computed per node from in-RAM data |
| 🟢 implemented | `key_columns` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `path` | yson | yes | computed per node from in-RAM data |
| 🟢 implemented | `resource_usage` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `row_count` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `schema` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `schema_mode` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `sorted` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `sorted_by` | yson | yes | computed for table nodes from in-RAM data |
| 🟢 implemented | `type` | yson | yes | computed per node from in-RAM data |
| 🟢 implemented | `uncompressed_data_size` | yson | yes | computed for table nodes from in-RAM data |
| 🟡 constant | `access_time` | yson |  | same fixed value for every node |
| 🟡 constant | `account` | yson |  | same fixed value for every node |
| 🟡 constant | `acl` | yson |  | same fixed value for every node |
| 🟡 constant | `attribute_revision` | yson |  | same fixed value for every node |
| 🟡 constant | `compression_codec` | yson |  | same fixed value for every table node |
| 🟡 constant | `content_revision` | yson |  | same fixed value for every node |
| 🟡 constant | `creation_time` | yson |  | same fixed value for every node |
| 🟡 constant | `effective_acl` | yson |  | same fixed value for every node |
| 🟡 constant | `erasure_codec` | yson |  | same fixed value for every table node |
| 🟡 constant | `has_row_level_ace` | yson |  | same fixed value for every node |
| 🟡 constant | `in_memory_mode` | yson |  | same fixed value for every table node |
| 🟡 constant | `inherit_acl` | yson |  | same fixed value for every node |
| 🟡 constant | `modification_time` | yson |  | same fixed value for every node |
| 🟡 constant | `opaque` | yson |  | same fixed value for every node |
| 🟡 constant | `opaque_attribute_keys` | yson |  | same fixed value for every node |
| 🟡 constant | `optimize_for` | yson |  | same fixed value for every table node |
| 🟡 constant | `owner` | yson |  | same fixed value for every node |
| 🟡 constant | `revision` | yson |  | same fixed value for every node |
| 🟡 constant | `tablet_state` | yson |  | same fixed value for every table node |
| 🟡 constant | `user_attribute_keys` | yson |  | same fixed value for every node |
| 🟡 constant | `user_attributes` | yson |  | same fixed value for every node |
| ⚪ unused | `_format` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_read_schema` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_restore_path` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_yql_key_meta` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_yql_op_id` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_yql_row_spec` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_yql_runner` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_yql_subkey_meta` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_yql_type` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `_yql_value_meta` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `annotation` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `annotation_path` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `atomicity` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `broken` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `cluster_name` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `compression_ratio` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `default_disk_space` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `disk_space` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `effective_expiration` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `enable_dynamic_store_read` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `expiration_time` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `expiration_timeout` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `key` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `leader_controller_address` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `lock_count` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `lock_mode` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `locks` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `mode` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `monitoring_cluster` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `monitoring_project` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `pipeline_format_version` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `primary_medium` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `queue_static_export_destination` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `remount_needed_tablet_count` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `replica_path` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `replicated_table_options` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `replication_factor` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `security_tags` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `start_time` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `state` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `tablet_cell_bundle` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `tablet_count` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `tablet_error_count` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `target_path` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `timeout` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `title` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `treat_as_queue_consumer` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `treat_as_queue_producer` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |
| ⚪ unused | `unmerged_row_count` | yson |  | requested by UI, absent in mock; per-attribute code-500 error is tolerated |

## table-schema-column

One column in the @schema attribute ({$attributes: {strict, unique_keys}, $value: [columns]}).

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `name` | string | yes | column name |
| 🟢 implemented | `sort_order` | string |  | 'ascending' on key columns; drives key icons and @key_columns |
| 🟢 implemented | `type` | string | yes | legacy type (int64, string, double, boolean, any) |
| 🟢 implemented | `type_v3` | map | yes | e.g. {type_name: optional, item: int64} |
| ⚪ unused | `aggregate` | string |  | dynamic tables only |
| ⚪ unused | `expression` | string |  | computed columns (dynamic tables) |
| ⚪ unused | `group` | string |  | column groups |
| ⚪ unused | `required` | bool |  | UI renders it when present; mock omits |

## web-json-cell

Scalar cell encoding inside web_json rows (value_format schemaless).

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `$type` | string | yes | int64/uint64/double/boolean/string/any |
| 🟢 implemented | `$value` | string | yes | stringified value; JSON-encoded for 'any' |
| ⚪ unused | `$incomplete` | bool |  | set when value truncated by field_weight_limit; mock never truncates |
| ⚪ unused | `$tag` | string |  | yql value_format only |
| ⚪ unused | `b64` | bool |  | yql value_format only (val/inc/b64 convention) |

## web-json-response

read_table response body in output_format web_json (the table viewer format).

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `all_column_names` | list<string> | yes | sorted ascending; drives the column selector |
| 🟢 implemented | `incomplete_all_column_names` | string | yes | STRING 'true'/'false' |
| 🟢 implemented | `incomplete_columns` | string | yes | STRING 'true'/'false', not boolean |
| 🟢 implemented | `rows` | list<map<column, web-json-cell>> | yes | row slice per requested range |
| ⚪ unused | `yql_type_registry` | list |  | only with value_format: yql; UI works without it (schemaless mode) |

## whoami-response

Body of GET /auth/whoami — the identity/CSRF endpoint used by cluster bootstrap and, when password auth is enabled, by UI-server request authentication.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `csrf_token` | string | yes | must be truthy or UI blocks with PRELOAD_ERROR.AUTHENTICATION; re-sent as X-Csrf-Token for credentialed requests, while the wrapper omits it when cluster authentication is 'none' |
| 🟢 implemented | `login` | string | yes | resolved from cookie/token; 'iceberg' for anonymous |
| 🟢 implemented | `real_login` | string | yes | same as login in mock |
| 🟢 implemented | `realm` | string | yes | 'cypress_cookie' for cookie auth, 'mock' otherwise |

## yt-error

YT TError JSON envelope; body of every error response, mirrored in X-YT-Error header; nested via inner_errors.

| Status | Field | Type | Required | Description |
|--------|-------|------|----------|-------------|
| 🟢 implemented | `attributes` | map | yes | extra context (path, code, ...) |
| 🟢 implemented | `code` | int | yes | numeric YT error code; the mock uses 1 (generic, incl. wrong-password /login, matching the real proxy), 500 (resolve/NODE_DOES_NOT_EXIST), and mock-only 900/901 for API-command auth/CSRF failures. The real proxy uses native auth codes such as 111/110 for cookie-auth failures |
| 🟢 implemented | `message` | string | yes | human-readable message |
| 🟡 constant | `inner_errors` | list<yt-error> | yes | mock always returns [] |
