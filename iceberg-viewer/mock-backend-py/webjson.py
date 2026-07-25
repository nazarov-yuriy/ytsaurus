"""Encoders for the JSON representations the YT HTTP proxy uses.

Python port of ../mock-backend/webjson.js — output is kept identical to the Node
implementation (JS number stringification included).

 - "annotated JSON" for structured command results (get/list):
     YSON attributes become {"$attributes": {...}, "$value": ...}
 - typed annotation for output_format {"$value":"json","$attributes":
     {"annotate_with_types":true,"stringify":true}}: every scalar becomes
     {"$type": "...", "$value": "<stringified>"}
 - "web_json" output format for read_table rows
"""
import json


def js_num_str(v) -> str:
    """JS String(number): integer-valued floats have no decimal point."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v) if isinstance(v, float) else str(v)


def js_json_dumps(v) -> str:
    """JSON.stringify(): compact separators, insertion order preserved."""
    return json.dumps(v, separators=(',', ':'), ensure_ascii=False)


# ---- annotated JSON (structured command output) ---------------------------

def annotated(v):
    """Encode a value as YT annotated JSON. Wraps {$attributes,$value} nodes as-is."""
    if v is None:
        return None
    if isinstance(v, list):
        return [annotated(x) for x in v]
    if isinstance(v, dict):
        if '$value' in v:
            out = {'$value': annotated(v['$value'])}
            # JS truthiness: an empty {} $attributes map is kept, only null/absent drops.
            if v.get('$attributes') is not None:
                out['$attributes'] = {k: annotated(x) for k, x in v['$attributes'].items()}
            return out
        return {k: annotated(x) for k, x in v.items()}
    return v


def typed_annotate(v):
    """Wrap every scalar as {$type,$value:str}; keep map/list shape and
    {$attributes,$value} wrappers (both parts annotated)."""
    if v is None:
        return None
    if isinstance(v, list):
        return [typed_annotate(x) for x in v]
    if isinstance(v, dict):
        if '$value' in v or '$attributes' in v:
            out = {}
            if v.get('$attributes') is not None:
                out['$attributes'] = {k: typed_annotate(x) for k, x in v['$attributes'].items()}
            out['$value'] = typed_annotate(v.get('$value'))
            return out
        return {k: typed_annotate(x) for k, x in v.items()}
    if isinstance(v, bool):
        return {'$type': 'boolean', '$value': 'true' if v else 'false'}
    if isinstance(v, int):
        return {'$type': 'int64', '$value': str(v)}
    if isinstance(v, float):
        if v.is_integer():
            return {'$type': 'int64', '$value': str(int(v))}
        return {'$type': 'double', '$value': js_num_str(v)}
    return {'$type': 'string', '$value': str(v)}


# ---- web_json -------------------------------------------------------------

def web_json_scalar(value, column_type):
    if value is None:
        return None
    if column_type in ('int64', 'int32', 'int16', 'int8', 'uint64', 'uint32', 'uint16', 'uint8'):
        return {'$type': column_type, '$value': js_num_str(value)}
    if column_type == 'double':
        return {'$type': 'double', '$value': js_num_str(value)}
    if column_type == 'boolean':
        return {'$type': 'boolean', '$value': 'true' if value else 'false'}
    if column_type in ('string', 'utf8'):
        return {'$type': 'string', '$value': str(value)}
    if column_type in ('any', 'yson'):
        # Complex values are emitted inline as a JSON string (schemaless web_json).
        return {'$type': 'any', '$value': js_json_dumps(value)}
    return {'$type': 'string', '$value': str(value)}


def web_json_body(schema, rows, start_row=0, row_limit=50,
                  column_names=None, max_selected_column_count=50,
                  max_all_column_names_count=2000):
    """Build the full web_json read_table response body.

    column_names, when present, fully replaces max_selected_column_count
    (the UI's column-discovery preload sends column_names: [] with range [#0:#0]).
    """
    all_columns = [c['name'] for c in schema]
    if isinstance(column_names, list):
        selected = [n for n in all_columns if n in column_names]
    else:
        selected = all_columns[:max_selected_column_count]
    row_slice = rows[start_row:start_row + row_limit]
    type_by_name = {c['name']: c['type'] for c in schema}
    return {
        'rows': [
            {name: web_json_scalar(row.get(name), type_by_name[name]) for name in selected}
            for row in row_slice
        ],
        # Note: these two are strings "true"/"false" on the wire, not booleans.
        'incomplete_columns': 'true' if len(selected) < len(all_columns) else 'false',
        'incomplete_all_column_names': 'true' if len(all_columns) > max_all_column_names_count else 'false',
        'all_column_names': sorted(all_columns)[:max_all_column_names_count],
    }
