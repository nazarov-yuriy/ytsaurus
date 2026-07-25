"""JSON encoders matching the YT HTTP proxy wire format (port of webjson.js)."""
import json

INT_TYPES = {'int64', 'int32', 'int16', 'int8', 'uint64', 'uint32', 'uint16', 'uint8'}


def js_num_str(v):
    # JS String(number): integer-valued floats print without a decimal point.
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def annotated(v):
    """YT annotated JSON: {$attributes,$value} wrappers pass through (empty {} kept)."""
    if isinstance(v, list):
        return [annotated(x) for x in v]
    if isinstance(v, dict):
        if '$value' in v:
            out = {'$value': annotated(v['$value'])}
            if v.get('$attributes') is not None:
                out['$attributes'] = annotated(v['$attributes'])
            return out
        return {k: annotated(x) for k, x in v.items()}
    return v


def typed_annotate(v):
    """annotate_with_types+stringify: every scalar becomes {$type,$value:str}."""
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
        return {'$type': 'boolean', '$value': str(v).lower()}
    if isinstance(v, (int, float)):
        is_double = isinstance(v, float) and not v.is_integer()
        return {'$type': 'double' if is_double else 'int64', '$value': js_num_str(v)}
    return {'$type': 'string', '$value': str(v)}


def web_json_scalar(v, t):
    if v is None:
        return None
    if t == 'boolean':
        return {'$type': 'boolean', '$value': str(v).lower()}
    if t in ('any', 'yson'):
        return {'$type': 'any', '$value': json.dumps(v, separators=(',', ':'), ensure_ascii=False)}
    if t in INT_TYPES or t == 'double':
        return {'$type': t, '$value': js_num_str(v)}
    return {'$type': 'string', '$value': str(v)}


def web_json_body(schema, rows, start_row=0, row_limit=50, column_names=None,
                  max_selected_column_count=50, max_all_column_names_count=2000):
    """read_table web_json body; column_names replaces max_selected_column_count."""
    all_columns = [c['name'] for c in schema]
    if isinstance(column_names, list):
        selected = [n for n in all_columns if n in column_names]
    else:
        selected = all_columns[:max_selected_column_count]
    types = {c['name']: c['type'] for c in schema}
    return {
        'rows': [{n: web_json_scalar(row.get(n), types[n]) for n in selected}
                 for row in rows[start_row:start_row + row_limit]],
        # both flags are strings on the wire, not booleans
        'incomplete_columns': str(len(selected) < len(all_columns)).lower(),
        'incomplete_all_column_names': str(len(all_columns) > max_all_column_names_count).lower(),
        'all_column_names': sorted(all_columns)[:max_all_column_names_count],
    }
