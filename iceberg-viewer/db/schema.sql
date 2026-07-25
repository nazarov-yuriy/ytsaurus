-- API catalog for the YTsaurus UI <-> backend protocol.
-- Populated from docs/*.inventory.json by sync.py; docs/API-INDEX.md is generated from it.

PRAGMA foreign_keys = ON;

-- One row per HTTP endpoint / driver command exposed to the UI.
CREATE TABLE IF NOT EXISTS endpoints (
    id              INTEGER PRIMARY KEY,
    layer           TEXT NOT NULL CHECK (layer IN ('ui-server', 'proxy')),
    api_version     TEXT,                          -- 'v3' | 'v4' | NULL for non-command endpoints
    command         TEXT,                          -- driver command name (get, list, read_table, ...)
    method          TEXT NOT NULL,                 -- GET/POST/PUT/ANY
    path            TEXT NOT NULL,                 -- URL path or pattern
    description     TEXT,
    needed_for_mock INTEGER NOT NULL DEFAULT 0,    -- 1 = mock backend must implement it
    -- implemented = dynamic behavior backed by in-RAM data (reimplement over Iceberg)
    -- constant    = fixed/stubbed response (keep as-is)
    -- unused      = not needed for the Iceberg viewer, not implemented in the mock
    support_status  TEXT CHECK (support_status IN ('implemented', 'constant', 'unused')),
    source_file     TEXT NOT NULL,                 -- which inventory json it came from
    UNIQUE (layer, method, path, command)
);

-- Query params, headers, cookies, body fields of a request or response.
CREATE TABLE IF NOT EXISTS endpoint_params (
    id          INTEGER PRIMARY KEY,
    endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    direction   TEXT NOT NULL CHECK (direction IN ('request', 'response')),
    kind        TEXT NOT NULL CHECK (kind IN ('query', 'header', 'cookie', 'body', 'param', 'status')),
    name        TEXT NOT NULL,
    value       TEXT,                              -- example value / type / schema snippet
    UNIQUE (endpoint_id, direction, kind, name)
);

-- Source references (file:line) backing an endpoint's documentation.
CREATE TABLE IF NOT EXISTS source_refs (
    id          INTEGER PRIMARY KEY,
    endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    ref         TEXT NOT NULL,
    UNIQUE (endpoint_id, ref)
);

-- Named payload schemas / type conventions (web_json value encoding, YT error object,
-- attribute-annotated JSON, table schema type_v3, ...).
CREATE TABLE IF NOT EXISTS schemas (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    definition  TEXT                               -- JSON schema / prose definition
);

CREATE TABLE IF NOT EXISTS schema_fields (
    id        INTEGER PRIMARY KEY,
    schema_id INTEGER NOT NULL REFERENCES schemas(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    type      TEXT,
    required  INTEGER NOT NULL DEFAULT 0,
    support_status TEXT CHECK (support_status IN ('implemented', 'constant', 'unused')),
    description TEXT,
    UNIQUE (schema_id, name)
);

-- Tracks which unstructured MD file documents which endpoint, for MD<->DB sync checks.
CREATE TABLE IF NOT EXISTS md_coverage (
    id          INTEGER PRIMARY KEY,
    endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    md_file     TEXT NOT NULL,
    mentioned   INTEGER NOT NULL DEFAULT 0,        -- 1 if the MD file mentions the endpoint path/command
    UNIQUE (endpoint_id, md_file)
);
