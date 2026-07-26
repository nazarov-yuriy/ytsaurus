"""JSON encoders matching the YT HTTP proxy wire format."""
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


def _yson_quote(v):
    """Quote UTF-8 bytes exactly like NYson::EscapeC."""
    data = str(v).encode()
    out = ['"']
    for index, byte in enumerate(data):
        next_byte = data[index + 1] if index + 1 < len(data) else 0
        if byte == ord('"'):
            out.append('\\"')
        elif byte == ord('\\'):
            out.append('\\\\')
        elif 32 <= byte <= 126:
            out.append(chr(byte))
        elif byte == ord('\r'):
            out.append('\\r')
        elif byte == ord('\n'):
            out.append('\\n')
        elif byte == ord('\t'):
            out.append('\\t')
        elif byte < 8 and not ord('0') <= next_byte <= ord('7'):
            out.append(f'\\{byte:o}')
        elif not (ord('0') <= next_byte <= ord('9')
                  or ord('A') <= next_byte <= ord('F')
                  or ord('a') <= next_byte <= ord('f')):
            out.append(f'\\x{byte:02x}')
        else:
            out.append(f'\\{byte:03o}')
    out.append('"')
    return ''.join(out)


def yson_text(v):
    """Minimal YSON text encoding (maps/lists/scalars) for X-YT-Error-Format: yson."""
    if v is None:
        return '#'
    if v is True or v is False:
        return '%true' if v else '%false'
    if isinstance(v, (int, float)):
        return js_num_str(v)
    if isinstance(v, list):
        return '[' + ''.join(yson_text(x) + ';' for x in v) + ']'
    if isinstance(v, dict):
        return '{' + ''.join(yson_text(k) + '=' + yson_text(x) + ';' for k, x in v.items()) + '}'
    return _yson_quote(v)


# value_format=yql: names from web_json_writer.cpp GetSimpleYqlTypeName (Any -> Yson).
YQL_TYPE_NAMES = {'int64': 'Int64', 'int32': 'Int32', 'int16': 'Int16', 'int8': 'Int8',
                  'uint64': 'Uint64', 'uint32': 'Uint32', 'uint16': 'Uint16', 'uint8': 'Uint8',
                  'double': 'Double', 'boolean': 'Boolean', 'string': 'String', 'utf8': 'Utf8',
                  'any': 'Yson', 'yson': 'Yson'}


def yql_cell(v, t, type_index):
    """Cell = [value, "<registry index>"]; optional present -> [inner], null stays null.
    Numbers stringified, booleans native JSON, any/Yson = {"val": <$type/$value tree>}."""
    if v is None:
        return [None, str(type_index)]
    if t == 'boolean':
        inner = bool(v)
    elif t in ('any', 'yson'):
        inner = {'val': typed_annotate(v)}
    elif t in INT_TYPES or t == 'double':
        inner = js_num_str(v)
    else:
        inner = str(v)
    return [[inner], str(type_index)]


def web_json_body(schema, rows, start_row=0, row_limit=50, column_names=None,
                  max_selected_column_count=50, max_all_column_names_count=2000,
                  value_format=None):
    """read_table web_json body; column_names replaces max_selected_column_count."""
    all_columns = [c['name'] for c in schema]
    if isinstance(column_names, list):
        selected = [n for n in all_columns if n in column_names]
    else:
        selected = all_columns[:max_selected_column_count]
    types = {c['name']: c['type'] for c in schema}
    row_slice = rows[start_row:start_row + row_limit]

    if value_format == 'yql':
        registry, index_of, col_index = [], {}, {}
        for n in selected:
            yql_type = ['OptionalType', ['DataType', YQL_TYPE_NAMES.get(types[n], 'String')]]
            key = json.dumps(yql_type)
            if key not in index_of:
                index_of[key] = len(registry)
                registry.append(yql_type)
            col_index[n] = index_of[key]
        out_rows = [{n: yql_cell(row.get(n), types[n], col_index[n]) for n in selected}
                    for row in row_slice]
    else:
        registry = None
        out_rows = [{n: web_json_scalar(row.get(n), types[n]) for n in selected}
                    for row in row_slice]

    body = {
        'rows': out_rows,
        # both flags are strings on the wire, not booleans
        'incomplete_columns': str(len(selected) < len(all_columns)).lower(),
        'incomplete_all_column_names': str(len(all_columns) > max_all_column_names_count).lower(),
        'all_column_names': sorted(all_columns)[:max_all_column_names_count],
    }
    if registry is not None:
        body['yql_type_registry'] = registry
    return body
