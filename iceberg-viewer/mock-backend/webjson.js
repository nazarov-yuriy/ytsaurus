// Encoders for the JSON representations the YT HTTP proxy uses:
//  - "annotated JSON" for structured command results (get/list):
//      YSON attributes become {"$attributes": {...}, "$value": ...}
//  - "web_json" output format for read_table: every scalar is
//      {"$type": "<yt type>", "$value": "<stringified>"} (+"$incomplete": true when truncated)
// Verified against yt/yt/client/formats/web_json_writer.cpp and the docs in ../docs/.

'use strict';

// ---- annotated JSON (structured command output) ---------------------------

function ytTypeOf(v) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'boolean') return 'boolean';
  if (typeof v === 'number') return Number.isInteger(v) ? 'int64' : 'double';
  if (typeof v === 'bigint') return 'int64';
  if (typeof v === 'string') return 'string';
  return 'any';
}

// Encode a JS value as YT "annotated JSON". Wraps {$attributes,$value} nodes as-is.
function annotated(v) {
  if (v === null || v === undefined) return null;
  if (Array.isArray(v)) return v.map(annotated);
  if (typeof v === 'object') {
    if ('$value' in v) {
      const out = {$value: annotated(v.$value)};
      if (v.$attributes) out.$attributes = mapValues(v.$attributes, annotated);
      return out;
    }
    return mapValues(v, annotated);
  }
  return v;
}

function mapValues(obj, fn) {
  const out = {};
  for (const [k, val] of Object.entries(obj)) out[k] = fn(val);
  return out;
}

// ---- web_json -------------------------------------------------------------

function webJsonScalar(value, columnType) {
  if (value === null || value === undefined) return null;
  switch (columnType) {
    case 'int64':
    case 'int32':
    case 'int16':
    case 'int8':
    case 'uint64':
    case 'uint32':
    case 'uint16':
    case 'uint8':
      return {$type: columnType, $value: String(value)};
    case 'double':
      return {$type: 'double', $value: String(value)};
    case 'boolean':
      return {$type: 'boolean', $value: value ? 'true' : 'false'};
    case 'string':
    case 'utf8':
      return {$type: 'string', $value: String(value)};
    case 'any':
    case 'yson':
      // Complex values are emitted inline (value_format=yql renders differently;
      // the default "schemaless" web_json emits {"$type":"any","$value":<json>}).
      return {$type: 'any', $value: JSON.stringify(value)};
    default:
      return {$type: 'string', $value: String(value)};
  }
}

// Minimal YSON text encoding (maps/lists/scalars) for X-YT-Error-Format: yson.
function ysonText(v) {
  if (v === null || v === undefined) return '#';
  if (typeof v === 'boolean') return v ? '%true' : '%false';
  if (typeof v === 'number') return String(v);
  if (Array.isArray(v)) return '[' + v.map((x) => ysonText(x) + ';').join('') + ']';
  if (typeof v === 'object') {
    return '{' + Object.entries(v).map(([k, x]) => ysonText(k) + '=' + ysonText(x) + ';').join('') + '}';
  }
  return '"' + String(v).split('\\').join('\\\\').split('"').join('\\"') + '"';
}

// Build the full web_json read_table response body.
// column_names, when present, fully replaces max_selected_column_count
// (the UI's column-discovery preload sends column_names: [] with range [#0:#0]).

// value_format=yql: names from web_json_writer.cpp GetSimpleYqlTypeName (Any -> Yson).
const YQL_TYPE_NAMES = {
  int64: 'Int64', int32: 'Int32', int16: 'Int16', int8: 'Int8',
  uint64: 'Uint64', uint32: 'Uint32', uint16: 'Uint16', uint8: 'Uint8',
  double: 'Double', boolean: 'Boolean', string: 'String', utf8: 'Utf8',
  any: 'Yson', yson: 'Yson',
};

// Cell = [value, "<registry index>"]; optional present -> [inner], null stays null.
// Numbers stringified, booleans native JSON, any/Yson = {"val": <$type/$value tree>}.
function yqlCell(v, t, typeIndex) {
  if (v === null || v === undefined) return [null, String(typeIndex)];
  let inner;
  if (t === 'boolean') inner = Boolean(v);
  else if (t === 'any' || t === 'yson') inner = {val: typedAnnotate(v)};
  else inner = String(v);  // numbers stringify identically to strings in JS
  return [[inner], String(typeIndex)];
}

function webJsonBody(schema, rows, {
  startRow = 0,
  rowLimit = 50,
  columnNames,
  maxSelectedColumnCount = 50,
  maxAllColumnNamesCount = 2000,
  valueFormat,
} = {}) {
  const allColumns = schema.map((c) => c.name);
  const selected = Array.isArray(columnNames)
    ? allColumns.filter((n) => columnNames.includes(n))
    : allColumns.slice(0, maxSelectedColumnCount);
  const slice = rows.slice(startRow, startRow + rowLimit);
  const typeByName = Object.fromEntries(schema.map((c) => [c.name, c.type]));

  let outRows;
  let registry = null;
  if (valueFormat === 'yql') {
    registry = [];
    const indexOf = {};
    const colIndex = {};
    for (const n of selected) {
      const yqlType = ['OptionalType', ['DataType', YQL_TYPE_NAMES[typeByName[n]] || 'String']];
      const key = JSON.stringify(yqlType);
      if (!(key in indexOf)) {
        indexOf[key] = registry.length;
        registry.push(yqlType);
      }
      colIndex[n] = indexOf[key];
    }
    outRows = slice.map((row) =>
      Object.fromEntries(selected.map((n) => [n, yqlCell(row[n], typeByName[n], colIndex[n])]))
    );
  } else {
    outRows = slice.map((row) =>
      Object.fromEntries(selected.map((name) => [name, webJsonScalar(row[name], typeByName[name])]))
    );
  }

  const body = {
    rows: outRows,
    // Note: these two are strings "true"/"false" on the wire, not booleans.
    incomplete_columns: String(selected.length < allColumns.length),
    incomplete_all_column_names: String(allColumns.length > maxAllColumnNamesCount),
    all_column_names: [...allColumns].sort().slice(0, maxAllColumnNamesCount),
  };
  if (registry !== null) body.yql_type_registry = registry;
  return body;
}

// Typed annotation for output_format {"$value":"json","$attributes":{"annotate_with_types":true,"stringify":true}}:
// every scalar becomes {"$type": "...", "$value": "<stringified>"}. Maps/lists keep
// their shape; {$attributes,$value} wrappers are preserved with both parts annotated.
function typedAnnotate(v) {
  if (v === null || v === undefined) return null;
  if (Array.isArray(v)) return v.map(typedAnnotate);
  if (typeof v === 'object') {
    if ('$value' in v || '$attributes' in v) {
      const out = {};
      if (v.$attributes) out.$attributes = mapValues(v.$attributes, typedAnnotate);
      out.$value = typedAnnotate('$value' in v ? v.$value : null);
      return out;
    }
    return mapValues(v, typedAnnotate);
  }
  if (typeof v === 'boolean') return {$type: 'boolean', $value: v ? 'true' : 'false'};
  if (typeof v === 'number') {
    return Number.isInteger(v)
      ? {$type: 'int64', $value: String(v)}
      : {$type: 'double', $value: String(v)};
  }
  if (typeof v === 'bigint') return {$type: 'int64', $value: String(v)};
  return {$type: 'string', $value: String(v)};
}

module.exports = {annotated, typedAnnotate, webJsonBody, webJsonScalar, ytTypeOf, ysonText};
