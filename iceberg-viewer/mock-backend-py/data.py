"""In-RAM fake cluster data: a Cypress-like tree of map nodes and static tables.

Tree-node creation order, ids, timestamps, and generated values are deterministic
so protocol tests and UI sessions see stable data.

This is the layer to re-implement on top of an Apache Iceberg catalog:
map nodes <-> namespaces, tables <-> Iceberg tables.
"""
import math
from datetime import datetime, timedelta, timezone

CLUSTER_ID = 'mock'

# ---- table schemas & rows -------------------------------------------------

TRIPS_SCHEMA = [
    {'name': 'trip_id', 'type': 'int64', 'type_v3': {'type_name': 'optional', 'item': 'int64'}, 'sort_order': 'ascending'},
    {'name': 'city', 'type': 'string', 'type_v3': {'type_name': 'optional', 'item': 'string'}},
    {'name': 'distance_km', 'type': 'double', 'type_v3': {'type_name': 'optional', 'item': 'double'}},
    {'name': 'started_at', 'type': 'string', 'type_v3': {'type_name': 'optional', 'item': 'string'}},
    {'name': 'is_completed', 'type': 'boolean', 'type_v3': {'type_name': 'optional', 'item': 'bool'}},
]


def _js_iso(dt: datetime) -> str:
    """JS Date#toISOString(): millisecond precision, 'Z' suffix."""
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def _make_trips_rows():
    rows = []
    for i in range(250):
        # JS: Math.round((3 + (i * 7919) % 400 / 10) * 100) / 100
        distance = math.floor((3 + ((i * 7919) % 400) / 10) * 100 + 0.5) / 100
        started = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=i % 180, hours=i % 24, minutes=(i * 13) % 60)
        rows.append({
            'trip_id': i + 1,
            'city': ['Amsterdam', 'Berlin', 'Copenhagen', 'Dublin'][i % 4],
            'distance_km': distance,
            'started_at': _js_iso(started),
            'is_completed': i % 5 != 0,
        })
    return rows


EVENTS_SCHEMA = [
    {'name': 'ts', 'type': 'uint64', 'type_v3': {'type_name': 'optional', 'item': 'uint64'}},
    {'name': 'user', 'type': 'string', 'type_v3': {'type_name': 'optional', 'item': 'string'}},
    {'name': 'payload', 'type': 'any', 'type_v3': {'type_name': 'optional', 'item': 'yson'}},
]


def _make_events_rows():
    return [{
        'ts': 1767225600 + i * 3600,
        'user': f'user_{i % 7}',
        'payload': {'kind': 'click', 'page': f'/p/{i}', 'coords': [i, i * 2]},
    } for i in range(40)]


# ---- Cypress tree ---------------------------------------------------------

_id_counter = 0


def _next_id() -> str:
    global _id_counter
    _id_counter += 1
    return f'0-{_id_counter:x}-10191-{0xabc0 + _id_counter:x}'


def _base_attrs(node_type: str) -> dict:
    now = '2026-07-25T10:00:00.000000Z'
    return {
        'id': _next_id(),
        'type': node_type,
        'creation_time': now,
        'modification_time': now,
        'access_time': now,
        'account': 'default',
        'owner': 'iceberg',
        'acl': [],
        'inherit_acl': True,
        'effective_acl': [],
        'revision': 1,
        'attribute_revision': 1,
        'content_revision': 1,
        'opaque': False,
        'has_row_level_ace': False,
        'path': None,  # filled below
    }


class Node:
    __slots__ = ('kind', 'attrs', 'children', 'rows', 'value')

    def __init__(self, kind, attrs, children=None, rows=None, value=None):
        self.kind = kind
        self.attrs = attrs
        self.children = children
        self.rows = rows
        self.value = value


def make_map_node() -> Node:
    return Node('map_node', _base_attrs('map_node'), children={})


def make_document(value) -> Node:
    """A leaf whose `get` returns a plain value (orchid-style version strings)."""
    return Node('document', _base_attrs('document'), value=value)


def make_live_table(schema, rows_fn) -> Node:
    """Table whose rows and row-derived attributes are computed per read."""
    node = make_table(schema, [])
    node.rows = rows_fn
    node.attrs.update(row_count=lambda: len(rows_fn()),
                      chunk_row_count=lambda: len(rows_fn()))
    return node


def make_table(schema, rows) -> Node:
    attrs = _base_attrs('table')
    key_columns = [c['name'] for c in schema if c.get('sort_order')]
    attrs.update({
        'dynamic': False,
        'sorted': any(c.get('sort_order') for c in schema),
        'key_columns': key_columns,
        'sorted_by': key_columns,
        'schema_mode': 'strong',
        'row_count': len(rows),
        'chunk_count': 1,
        'chunk_row_count': len(rows),
        'data_weight': len(rows) * 64,
        'compressed_data_size': len(rows) * 32,
        'uncompressed_data_size': len(rows) * 64,
        'resource_usage': {'disk_space': len(rows) * 64, 'chunk_count': 1, 'node_count': 1},
        'compression_codec': 'lz4',
        'erasure_codec': 'none',
        'optimize_for': 'scan',
        'in_memory_mode': 'none',
        'tablet_state': 'none',
        'schema': {
            '$attributes': {'strict': True, 'unique_keys': False},
            '$value': schema,
        },
    })
    return Node('table', attrs, rows=rows)


root = make_map_node()


def _insert(path: str, node: Node) -> Node:
    parts = [p for p in path.split('/') if p]
    cur = root
    for part in parts[:-1]:
        if part not in cur.children:
            cur.children[part] = make_map_node()
        cur = cur.children[part]
    cur.children[parts[-1]] = node
    return node


# Keep creation order stable because ids are sequential.
_insert('home', make_map_node())
_insert('home/iceberg', make_map_node())
_insert('home/iceberg/warehouse', make_map_node())
_insert('home/iceberg/warehouse/trips', make_table(TRIPS_SCHEMA, _make_trips_rows()))
_insert('home/iceberg/warehouse/events', make_table(EVENTS_SCHEMA, _make_events_rows()))
_insert('tmp', make_map_node())
_insert('sys', make_map_node())
# Cluster-params boot path: `list //sys/media` must succeed (medium list gate),
# `list //sys/primary_masters` may be empty.
_insert('sys/media', make_map_node())
_insert('sys/media/default', make_map_node())
_insert('sys/primary_masters', make_map_node())
# Operations/Users pages list these; empty maps beat resolve-error toasts.
_insert('sys/users', make_map_node())
_insert('sys/groups', make_map_node())
# Scheduling init reads //sys/pool_trees/@default_tree on every navigation load.
_pool_trees = _insert('sys/pool_trees', make_map_node())
_pool_trees.attrs['default_tree'] = 'physical'
_insert('sys/pool_trees/physical', make_map_node())
# Cluster-params boot also reads these versions (cluster-params.ts:149,173).
# They must be real "N.N.N-..." strings: ui >= 1.60 crashes on an undefined
# version (support.ts calls .match on it), so batch errors here are not an
# option. Keep these inserts last — earlier node ids must stay stable.
_insert('sys/scheduler/orchid/service/version', make_document('24.1.0-mock'))
_insert('sys/primary_masters/primary-master/orchid/service/version',
        make_document('24.1.0-mock'))

# The audit trail, browsable in the table viewer. Strict columns only —
# the schemaless `details` payload is never selected here (userdb.audit_rows),
# so its evolving contents cannot leak through the catalog API.
AUDIT_LOG_SCHEMA = [
    {'name': 'ts', 'type': 'string', 'type_v3': {'type_name': 'optional', 'item': 'string'}},
    {'name': 'login', 'type': 'string', 'type_v3': {'type_name': 'optional', 'item': 'string'}},
    {'name': 'endpoint', 'type': 'string', 'type_v3': {'type_name': 'optional', 'item': 'string'}},
]


def _audit_log_rows():
    import userdb  # deferred: keeps data.py importable without the store loaded
    return [{'ts': _js_iso(ts.astimezone(timezone.utc)), 'login': login, 'endpoint': endpoint}
            for ts, login, endpoint in userdb.audit_rows()]


_insert('sys/logs', make_map_node())
_insert('sys/logs/audit_log', make_live_table(AUDIT_LOG_SCHEMA, _audit_log_rows))


def _fill_paths(node: Node, path: str) -> None:
    """YT absolute paths start with '//' (root is '/')."""
    node.attrs['path'] = path or '/'
    for name, child in (node.children or {}).items():
        _fill_paths(child, f'{path or "/"}/{name}')


_fill_paths(root, '')


# ---- lookup ---------------------------------------------------------------

def resolve(ypath):
    """Resolves "//home/iceberg" or "//home/iceberg/@attr" style paths.

    Returns (node, attr_path) or None; attr_path is None when no /@ present.
    """
    if not isinstance(ypath, str) or not ypath.startswith('/'):
        return None
    attr_path = None
    at_idx = ypath.find('/@')
    if at_idx >= 0:
        attr_path = ypath[at_idx + 2:]
        ypath = ypath[:at_idx]
    cur = root
    for part in (p for p in ypath.split('/') if p):
        if cur.children is None or part not in cur.children:
            return None
        cur = cur.children[part]
    return cur, attr_path


# Users/sessions live in userdb.py (PostgreSQL or in-RAM).
