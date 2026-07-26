// Mock YTsaurus HTTP proxy serving in-RAM fake data to ytsaurus-ui.
// Run: node server.js [port]   (default 8000)
//
// Implements the minimal command surface for: login, navigation browsing and
// static-table viewing. Unknown requests are logged loudly (watch the console
// while clicking around the UI to discover missing endpoints).

'use strict';

const http = require('http');
const fs = require('fs');
const crypto = require('crypto');
const {URL} = require('url');
const {CLUSTER_ID, resolve, users} = require('./data');
const {annotated, typedAnnotate, webJsonBody, ysonText} = require('./webjson');

const PORT = Number(process.argv[2] || 8000);
const HOST = process.env.MOCK_HOST || `localhost:${PORT}`;
// When MOCK_RECORD is set, every request/response pair is appended as JSONL.
const RECORD_PATH = process.env.MOCK_RECORD || null;
const REQUIRE_AUTH = Boolean(process.env.MOCK_REQUIRE_AUTH);
const ROBOT_TOKEN = process.env.MOCK_ROBOT_TOKEN || '';

// MOCK_DELAY simulates a slow catalog: "1500" delays every data command by 1.5s,
// "read_table:5000,list:2000" per command. //sys paths are never delayed — the
// UI server's boot-path robot requests have a 5s timeout (see docs/timeouts.md).
const DELAYS = {};
for (const part of (process.env.MOCK_DELAY || '').split(',').filter(Boolean)) {
  const [cmd, ms] = part.includes(':') ? part.split(':') : [null, part];
  if (cmd) DELAYS[cmd] = Number(ms);
  else for (const c of ['get', 'list', 'exists', 'read_table']) DELAYS[c] = Number(ms);
}

function maybeDelay(command, params) {
  const ms = DELAYS[command] || 0;
  if (!ms || String((params || {}).path || '').startsWith('//sys')) return Promise.resolve();
  return new Promise((r) => setTimeout(r, ms));  // async: never blocks the event loop
}

function record(entry) {
  if (!RECORD_PATH) return;
  fs.appendFileSync(RECORD_PATH, JSON.stringify(entry) + '\n');
}

// ---- helpers --------------------------------------------------------------

function ytError(code, message, attributes = {}) {
  return {code, message, attributes, inner_errors: []};
}

function sendJson(res, status, body, extraHeaders = {}) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(data),
    ...extraHeaders,
  });
  res.end(data);
}

function gatherHeader(req, name) {
  const key = name.toLowerCase();
  if (req.headers[key] !== undefined) return String(req.headers[key]);
  const parts = [];
  for (let index = 0; index <= 1000; index++) {
    const part = req.headers[`${key}${index}`] ?? req.headers[`${key}-${index}`];
    if (part === undefined) break;
    if (index === 1000) throw new Error(`Too many ${name} header parts`);
    parts.push(String(part));
  }
  if (!parts.length) return null;
  const encoded = parts.join('');
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
    throw new Error(`Unable to parse ${name} header`);
  }
  return Buffer.from(encoded, 'base64').toString('utf8');
}

function parseErrorFormat(req) {
  function validate(name, annotateWithTypes) {
    if (!['json', 'web_json', 'yson'].includes(name)) {
      throw new Error(`Unsupported X-YT-Error-Format: ${name}`);
    }
    return {name, annotateWithTypes};
  }

  const raw = gatherHeader(req, 'X-YT-Error-Format');
  if (raw === null) return {name: 'json', annotateWithTypes: false};

  const headerFormat = String(req.headers['x-yt-header-format'] || 'json').trim();
  const headerFormatName = headerFormat.split('>').pop().trim().replace(/^"|"$/g, '');
  if (headerFormatName === 'yson') {
    const match = raw.trim().match(/^(?:<([^>]*)>)?([A-Za-z_][A-Za-z0-9_]*)$/);
    if (!match) throw new Error('Unable to parse X-YT-Error-Format header');
    return validate(
      match[2],
      /(?:^|[; ])annotate_with_types\s*=\s*%true(?:[; ]|$)/.test(match[1] || ''),
    );
  }
  if (headerFormatName !== 'json') throw new Error('Unsupported X-YT-Header-Format');

  let node;
  try {
    node = JSON.parse(raw);
  } catch {
    throw new Error('Unable to parse X-YT-Error-Format header');
  }
  if (typeof node === 'string') return validate(node, false);
  if (node && typeof node === 'object' && typeof node.$value === 'string') {
    return validate(node.$value, node.$attributes?.annotate_with_types === true);
  }
  throw new Error('Unable to parse X-YT-Error-Format header');
}

function formatErrorHeader(err, errorFormat) {
  if (errorFormat.name === 'yson') {
    return {text: ysonText(err), contentType: 'application/x-yt-yson-text'};
  }
  if (errorFormat.name === 'json' || errorFormat.name === 'web_json') {
    const obj = errorFormat.annotateWithTypes ? typedAnnotate(err) : err;
    return {text: JSON.stringify(obj), contentType: 'application/json'};
  }
  throw new Error(`Unsupported X-YT-Error-Format: ${errorFormat.name}`);
}

function escapeHeaderValue(value) {
  return JSON.stringify(String(value)).slice(1, -1).replace(
    /[\u007f-\uffff]/g,
    (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'),
  );
}

function sendYtError(req, res, status, err, extraHeaders = {}, errorFormat = null) {
  // X-YT-Error-Format governs the X-YT-Error header only. The real proxy
  // keeps the pre-flush response body as ordinary JSON (context.cpp).
  const formatted = formatErrorHeader(
    err, errorFormat || {name: 'json', annotateWithTypes: false});
  const body = JSON.stringify(err);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
    'X-YT-Error': formatted.text,
    'X-YT-Error-Content-Type': formatted.contentType,
    'X-YT-Response-Code': String(err.code),
    'X-YT-Response-Message': escapeHeaderValue(err.message),
    ...extraHeaders,
  });
  res.end(body);
}

function corsHeaders(req) {
  const origin = req.headers.origin;
  if (!origin) return {};
  return {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Methods': 'POST, PUT, GET, OPTIONS',
    'Access-Control-Allow-Headers': [
      'Content-Type', 'Accept', 'Authorization', 'Origin', 'Referer',
      'X-Csrf-Token', 'X-YT-Parameters', 'X-YT-Parameters-0', 'X-YT-Parameters-1',
      'X-YT-Response-Parameters', 'X-YT-Input-Format', 'X-YT-Output-Format',
      'X-YT-Error-Format', 'X-YT-Header-Format', 'X-YT-Suppress-Redirect', 'X-YT-Omit-Trailers',
      'X-YT-Request-Format-Options', 'X-YT-Response-Format-Options',
      'X-YT-Request-Id', 'X-YT-Correlation-Id', 'X-YT-Trace-Id', 'X-YT-User-Tag',
    ].join(', '),
    'Access-Control-Expose-Headers': [
      'Content-Type', 'X-YT-Error', 'X-YT-Response-Code', 'X-YT-Response-Message',
      'X-YT-Request-Id', 'X-YT-Proxy', 'X-YT-Trace-Id',
    ].join(', '),
    'Access-Control-Max-Age': '3600',
  };
}

function readBody(req) {
  return new Promise((resolveBody) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolveBody(Buffer.concat(chunks)));
  });
}

// Merge command parameters from query string, X-YT-Parameters header and JSON body.
function collectParams(req, url, bodyBuf) {
  const params = {};
  for (const [k, v] of url.searchParams) params[k] = v;
  const hdr = req.headers['x-yt-parameters'];
  if (hdr) {
    try {
      Object.assign(params, JSON.parse(hdr));
    } catch (e) {
      log(`  !! failed to parse X-YT-Parameters as JSON: ${hdr.slice(0, 200)}`);
    }
  }
  if (bodyBuf && bodyBuf.length) {
    const ct = req.headers['content-type'] || '';
    if (ct.includes('json') || !ct) {
      try {
        const body = JSON.parse(bodyBuf.toString('utf8'));
        if (body && typeof body === 'object') Object.assign(params, body);
      } catch (e) {
        /* raw data body (e.g. write_table) — ignore */
      }
    }
  }
  return params;
}

function log(...args) {
  console.log(new Date().toISOString().slice(11, 23), ...args);
}

// ---- auth -----------------------------------------------------------------

const sessions = new Map(); // cookie value -> {user, created, expires}
const SESSION_TTL_MS = Number(process.env.MOCK_COOKIE_TTL_SECONDS || 30 * 24 * 3600) * 1000;
const COOKIE_RENEWAL_MS = Number(process.env.MOCK_COOKIE_RENEWAL_SECONDS || 7 * 24 * 3600) * 1000;
const CSRF_TTL_MS = Number(process.env.MOCK_CSRF_TTL_SECONDS || 24 * 3600) * 1000;
const CSRF_SECRET = process.env.MOCK_CSRF_SECRET || crypto.randomBytes(32).toString('hex');

function makeCookie(user) {
  // GenerateCookieValue parity (cypress_cookie.cpp:47-53): 32 random bytes, hex.
  const value = crypto.randomBytes(32).toString('hex');
  const now = Date.now();
  sessions.set(value, {user, created: now, expires: now + SESSION_TTL_MS});
  return value;
}

function sessionInfo(value) {
  const info = sessions.get(value);
  return info && info.expires > Date.now() ? info : null;
}

function csrfTokenFor(user) {
  // SignCsrfToken parity: hex(hmac_sha256(secret, "user:unix_ts")) + ":" + unix_ts
  const ts = Math.floor(Date.now() / 1000);
  const sig = crypto.createHmac('sha256', CSRF_SECRET).update(`${user}:${ts}`).digest('hex');
  return `${sig}:${ts}`;
}

function checkCsrfToken(token, user) {
  const [sig, ts] = String(token || '').split(':');
  if (ts === undefined) return 'Malformed CSRF token';
  if (!/^\d+$/.test(ts) || Number(ts) * 1000 < Date.now() - CSRF_TTL_MS) return 'CSRF token expired';
  const expected = crypto.createHmac('sha256', CSRF_SECRET).update(`${user}:${ts}`).digest('hex');
  return tokenMatches(sig || '', expected) ? null : 'Invalid CSFR token signature';  // typo as in helpers.cpp:187
}

function renewalCookieHeader(auth, req) {
  // Cookie rotation (cypress_cookie_authenticator.cpp:164).
  if (!auth || !auth.viaCookie) return {};
  const cookies = Object.fromEntries(
    (req.headers.cookie || '').split(';').map((p) => p.trim().split('=').map(decodeURIComponent))
  );
  const info = sessionInfo(cookies['YTCypressCookie'] || '');
  if (!info || info.expires - Date.now() > COOKIE_RENEWAL_MS) return {};
  const value = makeCookie(auth.user);
  const expires = new Date(Date.now() + SESSION_TTL_MS).toUTCString();
  return {'Set-Cookie': `YTCypressCookie=${value}; Expires=${expires}; HttpOnly; Path=/`};
}

function tokenMatches(actual, expected) {
  const actualDigest = crypto.createHash('sha256').update(actual).digest();
  const expectedDigest = crypto.createHash('sha256').update(expected).digest();
  return crypto.timingSafeEqual(actualDigest, expectedDigest);
}

function authenticate(req) {
  // Cookie-based (password login flow)
  const cookies = Object.fromEntries(
    (req.headers.cookie || '').split(';').map((p) => p.trim().split('=').map(decodeURIComponent))
  );
  const yc = cookies['YTCypressCookie'];
  const info = yc && sessionInfo(yc);
  if (info) return {user: info.user, viaCookie: true};
  // Token-based
  const auth = req.headers.authorization || '';
  if (auth.startsWith('OAuth ')) {
    const token = auth.slice(6).trim();
    if (REQUIRE_AUTH) {
      if (token && ROBOT_TOKEN && tokenMatches(token, ROBOT_TOKEN)) {
        return {user: 'iceberg', viaCookie: false};
      }
      return null;
    }
    if (token) return {user: token in users ? token : 'iceberg', viaCookie: false};
  }
  if (REQUIRE_AUTH) return null;
  // Anonymous access: with `authentication: "none"` the UI node server sends no
  // credentials at all (mirrors require_authentication=false → root login).
  return {user: 'iceberg', viaCookie: false, anonymous: true};
}

// Returns an error message, or null when the request passes.
function checkCsrf(req, auth) {
  if (!auth || !auth.viaCookie) return null; // token auth needs no CSRF
  if (['GET', 'HEAD', 'OPTIONS'].includes(req.method)) return null;
  return checkCsrfToken(req.headers['x-csrf-token'], auth.user);
}

// ---- command implementations ---------------------------------------------

const COOKIE_STORE_PATH = '//sys/cypress_cookies';

// Virtual //sys/cypress_cookies/<value>[/<field>] view over the session store
// (cypress_cookie_store.cpp:282 keeps cookies by value under that map node).
function cookieStoreNode(path) {
  const rest = path.slice(COOKIE_STORE_PATH.length).replace(/^\/|\/$/g, '');
  if (!rest) {
    const out = {};
    for (const [c, i] of sessions.entries()) if (i.expires > Date.now()) out[c] = null;
    return out;
  }
  const [value, field] = [rest.split('/')[0], rest.split('/').slice(1).join('/')];
  const info = sessionInfo(value);
  if (!info) throw {status: 400, err: ytError(500, `Error resolving path ${path}`, {code: 500})};
  const record = {
    value, user: info.user, auth_source: 'password', password_revision: 0,
    expires_at: new Date(info.expires).toISOString().replace('Z', '000Z'),
  };
  if (!field) return record;
  if (!(field in record)) throw {status: 400, err: ytError(500, `Error resolving path ${path}`, {code: 500})};
  return record[field];
}


function attributesFor(node, requested) {
  const all = node.attrs;
  if (!requested) return {};
  const keys = Array.isArray(requested) ? requested : requested.keys || [];
  const out = {};
  for (const k of keys) {
    if (k in all && all[k] !== null && all[k] !== undefined) out[k] = all[k];
  }
  return out;
}

function nodeValue(node, params) {
  // Structured value of a node for `get` (map children as dict, tables as entity).
  if (node.kind === 'map_node') {
    const out = {};
    for (const [name, child] of Object.entries(node.children)) {
      out[name] = {
        $attributes: attributesFor(child, params.attributes),
        $value: child.kind === 'map_node' && Object.keys(child.children).length === 0 ? {} : null,
      };
      if (child.kind === 'map_node') {
        out[name].$value = {}; // do not expand deeply; UI lists children via `list`
      }
    }
    return out;
  }
  return null; // tables/files are entities
}

const commands = {
  get(params, auth) {
    const cookiePath = String(params.path || '');
    if (cookiePath === COOKIE_STORE_PATH || cookiePath.startsWith(COOKIE_STORE_PATH + '/')) {
      return cookieStoreNode(cookiePath);
    }
    const r = resolve(params.path);
    if (!r) throw {status: 400, err: ytError(500, `Error resolving path ${params.path}`, {path: params.path, code: 500})};
    const {node, attrPath} = r;
    if (attrPath !== null) {
      if (attrPath === '') {
        return {...node.attrs};
      }
      // Virtual attributes every Cypress node has (UI reads them on Attributes tabs).
      const VIRTUAL = {opaque_attribute_keys: [], user_attributes: {}, user_attribute_keys: []};
      const [head, ...rest] = attrPath.split('/');
      let v = head in VIRTUAL && !(head in node.attrs) ? VIRTUAL[head] : node.attrs[head];
      for (const k of rest) {
        if (v && typeof v === 'object') v = ('$value' in v ? v.$value : v)[k];
      }
      if (v === undefined) {
        throw {status: 400, err: ytError(500, `Attribute "${head}" is not found`, {code: 500})};
      }
      return v;
    }
    const value = nodeValue(node, params);
    const attrs = attributesFor(node, params.attributes);
    if (Object.keys(attrs).length) return {$attributes: attrs, $value: value};
    return value;
  },

  list(params, auth) {
    if (String(params.path || '') === COOKIE_STORE_PATH) {
      return [...sessions.entries()].filter(([, i]) => i.expires > Date.now()).map(([c]) => c).sort();
    }
    const r = resolve(params.path);
    if (!r || r.node.kind !== 'map_node') {
      throw {status: 400, err: ytError(500, `Error resolving path ${params.path}`, {path: params.path, code: 500})};
    }
    return Object.entries(r.node.children).map(([name, child]) => {
      const attrs = attributesFor(child, params.attributes);
      return Object.keys(attrs).length ? {$attributes: attrs, $value: name} : name;
    });
  },

  exists(params) {
    return Boolean(resolve(params.path));
  },

  read_table(params, auth) {
    const r = resolve(stripRanges(params.path));
    if (!r || r.node.kind !== 'table') {
      throw {status: 400, err: ytError(500, `Error resolving path ${params.path}`, {code: 500})};
    }
    const {start, limit} = rangeOf(params.path);
    const of = params.output_format;
    const ofName = typeof of === 'string' ? of : of && of.$value;
    const ofAttrs = (of && of.$attributes) || {};
    const schema = r.node.attrs.schema.$value;
    if (ofName === 'web_json') {
      return webJsonBody(schema, r.node.rows, {
        startRow: start,
        rowLimit: limit ?? 50,
        // column_names, when present, fully replaces max_selected_column_count.
        columnNames: ofAttrs.column_names,
        maxSelectedColumnCount: Number(ofAttrs.max_selected_column_count) || 50,
        maxAllColumnNamesCount: Number(ofAttrs.max_all_column_names_count) || 2000,
        valueFormat: ofAttrs.value_format,
      });
    }
    // json format fallback: newline-delimited rows
    return r.node.rows.slice(start, start + (limit ?? 50));
  },

  get_table_columnar_statistics(params) {
    const paths = params.paths || [];
    return paths.map(() => ({column_data_weights: {}, timestamp_total_weight: 0, legacy_chunks_data_weight: 0}));
  },

  // Authentication-ish commands
  whoami(params, auth) {
    return {login: auth.user, realm: 'mock'};
  },

  // Permission probes: allow everything (failures only produce error toasts anyway).
  check_permission(params, auth) {
    return {action: 'allow', object_id: '0-0-0-0', object_name: params.path, subject_id: '0-0-0-1', subject_name: auth.user};
  },
  check_permission_by_acl(params, auth) {
    return {action: 'allow', subject_id: '0-0-0-1', subject_name: auth.user, missing_subjects: []};
  },
  get_supported_features() {
    return {features: {compression_codecs: ['none', 'lz4'], erasure_codecs: ['none'], primitive_types: ['int64', 'uint64', 'double', 'boolean', 'string', 'any']}};
  },

  // Runs subrequests [{command, parameters}] and returns one result object each.
  async execute_batch(params, auth) {
    const out = [];
    for (const r of params.requests || []) {
      const impl = commands[r.command];
      if (!impl) {
        out.push({error: ytError(1, `Command ${r.command} is not registered in batch`)});
        continue;
      }
      try {
        await maybeDelay(r.command, r.parameters);
        out.push({output: await impl(r.parameters || {}, auth)});
      } catch (e) {
        out.push(e && e.err ? {error: e.err} : {error: ytError(1, String(e && e.message || e))});
      }
    }
    return out;
  },
};

// "//path[#10:#60]" → {path: "//path", start, limit}
function stripRanges(p) {
  return typeof p === 'object' && p !== null ? p.$value ?? p : String(p).replace(/\[.*\]$/, '');
}
function rangeOf(p) {
  if (typeof p === 'object' && p !== null && p.$attributes && p.$attributes.ranges) {
    const range = p.$attributes.ranges[0] || {};
    const start = range.lower_limit && range.lower_limit.row_index != null ? range.lower_limit.row_index : 0;
    const end = range.upper_limit && range.upper_limit.row_index != null ? range.upper_limit.row_index : undefined;
    return {start, limit: end === undefined ? undefined : end - start};
  }
  const m = String(p).match(/\[#(\d+):#(\d+)\]$/);
  if (m) return {start: Number(m[1]), limit: Number(m[2]) - Number(m[1])};
  return {start: 0, limit: undefined};
}

// ---- HTTP server ----------------------------------------------------------

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}`);
  const cors = corsHeaders(req);

  if (req.method === 'OPTIONS') {
    res.writeHead(200, cors);
    res.end();
    return;
  }

  const bodyBuf = await readBody(req);
  const p = url.pathname;
  log(req.method, p + (url.search || ''), bodyBuf.length ? `body=${bodyBuf.slice(0, 300)}` : '');

  if (RECORD_PATH) {
    const INTERESTING = ['authorization', 'cookie', 'x-csrf-token', 'content-type', 'accept', 'x-yt-correlation-id', 'x-yt-parameters', 'x-yt-input-format', 'x-yt-output-format', 'x-yt-header-format', 'x-yt-suppress-redirect'];
    const reqHeaders = {};
    for (const h of INTERESTING) if (req.headers[h]) reqHeaders[h] = req.headers[h];
    let reqBody = null;
    if (bodyBuf.length) {
      const text = bodyBuf.toString('utf8');
      try { reqBody = JSON.parse(text); } catch { reqBody = text.slice(0, 4000); }
    }
    const origEnd = res.end.bind(res);
    res.end = (data) => {
      let respBody = null;
      if (data) {
        const text = String(data);
        try { respBody = JSON.parse(text); } catch { respBody = text.slice(0, 4000); }
      }
      record({
        ts: new Date().toISOString(),
        method: req.method,
        path: p,
        query: url.search || '',
        request_headers: reqHeaders,
        request_body: reqBody,
        status: res.statusCode,
        response_body: respBody,
      });
      return origEnd(data);
    };
  }

  try {
    // ---- infrastructure endpoints ----
    if (p === '/ping' || p === '/ready') return void sendJson(res, 200, {}, cors);
    if (p === '/version' || p === '/service/version') {
      res.writeHead(200, {'Content-Type': 'text/plain', ...cors});
      return void res.end('mock-proxy-1.0.0');
    }
    if (p === '/hosts/all') {
      // System page expects objects here; empty list keeps it from crashing.
      return void sendJson(res, 200, [], cors);
    }
    if (p === '/hosts' || p.startsWith('/hosts/')) {
      // Role filtering like coordinator.cpp: this mock is one 'data'-role
      // proxy (the default role filter); other roles have no members.
      const role = url.searchParams.get('role') || 'data';
      return void sendJson(res, 200, role === 'data' ? [HOST] : [], cors);
    }
    if (p === '/api' || p === '/api/') {
      return void sendJson(res, 200, ['v3', 'v4'], cors);
    }

    // ---- password login: HTTP Basic auth to /login, per cypress_cookie_login.cpp.
    // Real proxy replies with empty 200 + Set-Cookie: YTCypressCookie (no SameSite).
    if (p === '/login') {
      const authorization = req.headers.authorization;
      if (authorization === undefined) {
        res.writeHead(401, {...cors, 'WWW-Authenticate': 'Basic', 'Content-Length': '0'});
        return void res.end();
      }

      const separator = authorization.indexOf(' ');
      if (separator === -1) {
        return void sendYtError(req, res, 400, ytError(
          1, 'Malformed "Authorization" header: failed to parse authorization method'), cors);
      }

      const method = authorization.slice(0, separator);
      const encodedCredentials = authorization.slice(separator + 1);
      if (method !== 'Basic') {
        return void sendYtError(
          req, res, 400, ytError(1, `Unsupported authorization method "${method}"`), cors);
      }
      if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/
        .test(encodedCredentials)) {
        return void sendYtError(
          req, res, 400, ytError(1, 'Failed to decode user credentials'), cors);
      }

      const credentials = Buffer.from(encodedCredentials, 'base64').toString('utf8');
      const colon = credentials.indexOf(':');
      if (colon === -1) {
        return void sendYtError(
          req, res, 400, ytError(1, 'Failed to parse user credentials'), cors);
      }
      const user = credentials.slice(0, colon);
      const password = credentials.slice(colon + 1);
      if (!(user in users) || users[user].password !== password) {
        // Real proxy masks the cause: generic code 1 (cypress_cookie_login.cpp:83).
        return void sendYtError(req, res, 401, ytError(1, 'Incorrect login or password'), {
          ...cors,
          'WWW-Authenticate': 'Basic',
        });
      }
      const cookie = makeCookie(user);
      const expires = new Date(Date.now() + 30 * 24 * 3600 * 1000).toUTCString();
      res.writeHead(200, {
        ...cors,
        'Set-Cookie': `YTCypressCookie=${cookie}; Expires=${expires}; HttpOnly; Path=/`,
        'Content-Length': '0',
      });
      return void res.end();
    }

    // ---- /auth/whoami: the single auth gate the UI server checks on every request.
    // Must succeed with a truthy csrf_token even without credentials (auth "none" mode).
    if (p === '/auth/whoami') {
      const auth = authenticate(req);
      if (!auth) {
        return void sendYtError(req, res, 401, ytError(900, 'Authentication failed'), cors);
      }
      const user = auth.user;
      return void sendJson(res, 200, {
        login: user,
        realm: auth && auth.viaCookie ? 'cypress_cookie' : 'mock',
        real_login: user,
        csrf_token: csrfTokenFor(user),
      }, {...cors, ...renewalCookieHeader(auth, req)});
    }

    // ---- API commands ----
    const m = p.match(/^\/api\/(v3|v4)\/(\w+)$/);
    if (m) {
      const [, version, command] = m;
      const auth = authenticate(req);
      if (!auth) {
        return void sendYtError(req, res, 401, ytError(900, 'Authentication failed'), cors);
      }
      const csrfError = checkCsrf(req, auth);
      if (csrfError) {
        // NRpc::EErrorCode::InvalidCsrfToken = 110 (core/rpc/public.h:207)
        return void sendYtError(req, res, 401, ytError(110, csrfError), cors);
      }
      const params = collectParams(req, url, bodyBuf);
      const impl = commands[command];
      if (!impl) {
        log(`  !! unimplemented command: ${command} params=${JSON.stringify(params).slice(0, 500)}`);
        return void sendYtError(req, res, 404, ytError(1, `Command ${command} is not registered`), cors);
      }
      let errorFormat;
      try {
        errorFormat = parseErrorFormat(req);
      } catch (e) {
        return void sendYtError(req, res, 400, ytError(1, e.message), cors);
      }
      let result;
      try {
        await maybeDelay(command, params);
        result = await impl(params, auth);
      } catch (e) {
        if (e && e.err) {
          return void sendYtError(req, res, e.status, e.err, cors, errorFormat);
        }
        throw e;
      }
      const RAW_OUTPUT = new Set(['read_table', 'get_table_columnar_statistics']);
      // The request's output_format governs the envelope: with annotate_with_types
      // every scalar in the result (including batch sub-results) is {$type,$value}.
      const of = params.output_format;
      const typed = Boolean(of && of.$attributes && of.$attributes.annotate_with_types);
      let payload = RAW_OUTPUT.has(command) ? result : (typed ? typedAnnotate(result) : annotated(result));
      if (version === 'v4' && (command === 'get' || command === 'list' || command === 'exists')) {
        payload = {value: payload};
      }
      return void sendJson(res, 200, payload, {...cors, 'X-YT-Proxy': HOST, ...renewalCookieHeader(auth, req)});
    }

    log(`  !! unhandled route`);
    sendYtError(req, res, 404, ytError(1, `No such route: ${p}`), cors);
  } catch (e) {
    log('  !! internal error', e);
    sendYtError(req, res, 500, ytError(1, String(e && e.message || e)), cors);
  }
});

server.listen(PORT, () => log(`mock YT proxy listening on http://${HOST}`));
