// In-RAM fake cluster data: a Cypress-like tree of map nodes and static tables.
// This is the layer you would later re-implement on top of an Apache Iceberg catalog:
// map nodes <-> namespaces, tables <-> Iceberg tables.

'use strict';

const CLUSTER_ID = 'mock';

// ---- table schemas & rows -------------------------------------------------

const tripsSchema = [
  {name: 'trip_id', type: 'int64', type_v3: {type_name: 'optional', item: 'int64'}, sort_order: 'ascending'},
  {name: 'city', type: 'string', type_v3: {type_name: 'optional', item: 'string'}},
  {name: 'distance_km', type: 'double', type_v3: {type_name: 'optional', item: 'double'}},
  {name: 'started_at', type: 'string', type_v3: {type_name: 'optional', item: 'string'}},
  {name: 'is_completed', type: 'boolean', type_v3: {type_name: 'optional', item: 'bool'}},
];

const tripsRows = Array.from({length: 250}, (_, i) => ({
  trip_id: i + 1,
  city: ['Amsterdam', 'Berlin', 'Copenhagen', 'Dublin'][i % 4],
  distance_km: Math.round((3 + (i * 7919) % 400 / 10) * 100) / 100,
  started_at: new Date(Date.UTC(2026, 0, 1 + (i % 180), i % 24, (i * 13) % 60)).toISOString(),
  is_completed: i % 5 !== 0,
}));

const eventsSchema = [
  {name: 'ts', type: 'uint64', type_v3: {type_name: 'optional', item: 'uint64'}},
  {name: 'user', type: 'string', type_v3: {type_name: 'optional', item: 'string'}},
  {name: 'payload', type: 'any', type_v3: {type_name: 'optional', item: 'yson'}},
];

const eventsRows = Array.from({length: 40}, (_, i) => ({
  ts: 1767225600 + i * 3600,
  user: `user_${i % 7}`,
  payload: {kind: 'click', page: `/p/${i}`, coords: [i, i * 2]},
}));

// ---- Cypress tree ---------------------------------------------------------

let idCounter = 0;
function nextId() {
  idCounter += 1;
  return `0-${idCounter.toString(16)}-10191-${(0xabc0 + idCounter).toString(16)}`;
}

function baseAttrs(type) {
  const now = '2026-07-25T10:00:00.000000Z';
  return {
    id: nextId(),
    type,
    creation_time: now,
    modification_time: now,
    access_time: now,
    account: 'default',
    owner: 'iceberg',
    acl: [],
    inherit_acl: true,
    effective_acl: [],
    revision: 1,
    attribute_revision: 1,
    content_revision: 1,
    opaque: false,
    has_row_level_ace: false,
    path: null, // filled below
  };
}

function makeMapNode() {
  return {kind: 'map_node', attrs: baseAttrs('map_node'), children: {}};
}

function makeTable(schema, rows) {
  const attrs = baseAttrs('table');
  Object.assign(attrs, {
    dynamic: false,
    sorted: schema.some((c) => c.sort_order),
    key_columns: schema.filter((c) => c.sort_order).map((c) => c.name),
    sorted_by: schema.filter((c) => c.sort_order).map((c) => c.name),
    schema_mode: 'strong',
    row_count: rows.length,
    chunk_count: 1,
    chunk_row_count: rows.length,
    data_weight: rows.length * 64,
    compressed_data_size: rows.length * 32,
    uncompressed_data_size: rows.length * 64,
    resource_usage: {disk_space: rows.length * 64, chunk_count: 1, node_count: 1},
    compression_codec: 'lz4',
    erasure_codec: 'none',
    optimize_for: 'scan',
    in_memory_mode: 'none',
    tablet_state: 'none',
    schema: {
      $attributes: {strict: true, unique_keys: false},
      $value: schema,
    },
  });
  return {kind: 'table', attrs, rows};
}

const root = makeMapNode();

function insert(path, node) {
  const parts = path.split('/').filter(Boolean);
  let cur = root;
  for (const part of parts.slice(0, -1)) {
    if (!cur.children[part]) cur.children[part] = makeMapNode();
    cur = cur.children[part];
  }
  cur.children[parts[parts.length - 1]] = node;
  return node;
}

insert('home', makeMapNode());
insert('home/iceberg', makeMapNode());
insert('home/iceberg/warehouse', makeMapNode());
insert('home/iceberg/warehouse/trips', makeTable(tripsSchema, tripsRows));
insert('home/iceberg/warehouse/events', makeTable(eventsSchema, eventsRows));
insert('tmp', makeMapNode());
insert('sys', makeMapNode());
// Cluster-params boot path: `list //sys/media` must succeed (medium list gate),
// `list //sys/primary_masters` may be empty.
insert('sys/media', makeMapNode());
insert('sys/media/default', makeMapNode());
insert('sys/primary_masters', makeMapNode());
// Scheduling init reads //sys/pool_trees/@default_tree on every navigation load.
const poolTrees = insert('sys/pool_trees', makeMapNode());
poolTrees.attrs.default_tree = 'physical';
insert('sys/pool_trees/physical', makeMapNode());

// Fill in path attributes. YT absolute paths start with "//" (root is "/").
(function fillPaths(node, path) {
  node.attrs.path = path || '/';
  for (const [name, child] of Object.entries(node.children || {})) {
    fillPaths(child, `${path || '/'}/${name}`);
  }
})(root, '');

// ---- lookup ---------------------------------------------------------------

// Resolves "//home/iceberg" or "//home/iceberg/@attr" style paths.
// Returns {node, attrPath} or null.
function resolve(ypath) {
  if (typeof ypath !== 'string' || !ypath.startsWith('/')) return null;
  let attrPath = null;
  const atIdx = ypath.indexOf('/@');
  if (atIdx >= 0) {
    attrPath = ypath.slice(atIdx + 2);
    ypath = ypath.slice(0, atIdx);
  }
  const parts = ypath.split('/').filter(Boolean);
  let cur = root;
  for (const part of parts) {
    if (!cur.children || !Object.prototype.hasOwnProperty.call(cur.children, part)) return null;
    cur = cur.children[part];
  }
  return {node: cur, attrPath};
}

const users = {
  iceberg: {password: 'iceberg'},
  root: {password: ''},
};

module.exports = {CLUSTER_ID, root, resolve, users};
