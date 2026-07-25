#!/usr/bin/env python3
"""Mock YTsaurus HTTP proxy serving in-RAM fake data to ytsaurus-ui.

Python port of ../mock-backend/server.js — same routes, same auth, same response
bodies (verified against the Node implementation by recordings/replay-diff.py).

Run: python3 server.py [port]   (default 8000)

Implements the minimal command surface for: login, navigation browsing and
static-table viewing. Unknown requests are logged loudly (watch the console
while clicking around the UI to discover missing endpoints).
"""
import base64
import json
import os
import random
import re
import socket
import string
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from data import resolve, users
from webjson import annotated, typed_annotate, web_json_body

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
HOST = os.environ.get('MOCK_HOST', f'localhost:{PORT}')
# When MOCK_RECORD is set, every request/response pair is appended as JSONL.
RECORD_PATH = os.environ.get('MOCK_RECORD')


def record(entry):
    if not RECORD_PATH:
        return
    with open(RECORD_PATH, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def log(*args):
    print(datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3], *args, flush=True)


def yt_error(code, message, attributes=None):
    return {'code': code, 'message': message, 'attributes': attributes or {}, 'inner_errors': []}


class CommandError(Exception):
    def __init__(self, status, err):
        self.status = status
        self.err = err


# ---- auth -----------------------------------------------------------------

sessions = {}  # cookie value -> username


def make_cookie(user):
    value = 'mock-' + user + '-' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=11))
    sessions[value] = user
    return value


def csrf_token_for(user):
    return f'csrf-{user}'


def authenticate(headers):
    cookies = {}
    for part in (headers.get('Cookie') or '').split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            cookies[k] = v
    yc = cookies.get('YTCypressCookie')
    if yc and yc in sessions:
        return {'user': sessions[yc], 'via_cookie': True}
    auth = headers.get('Authorization') or ''
    if auth.startswith('OAuth '):
        token = auth[6:].strip()
        if token:
            return {'user': token if token in users else 'iceberg', 'via_cookie': False}
    # Anonymous access: with `authentication: "none"` the UI node server sends no
    # credentials at all (mirrors require_authentication=false -> root login).
    return {'user': 'iceberg', 'via_cookie': False, 'anonymous': True}


def check_csrf(method, headers, auth):
    if not auth or not auth['via_cookie']:
        return True  # token/anonymous auth needs no CSRF
    if method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    return headers.get('X-Csrf-Token') == csrf_token_for(auth['user'])


# ---- command implementations ---------------------------------------------

def attributes_for(node, requested):
    if not requested:
        return {}
    keys = requested if isinstance(requested, list) else (requested or {}).get('keys', [])
    return {k: node.attrs[k] for k in keys if node.attrs.get(k) is not None}


def node_value(node, params):
    """Structured value of a node for `get` (map children as dict, tables as entity)."""
    if node.kind == 'map_node':
        out = {}
        for name, child in node.children.items():
            out[name] = {
                '$attributes': attributes_for(child, params.get('attributes')),
                # do not expand deeply; UI lists children via `list`
                '$value': {} if child.kind == 'map_node' else None,
            }
        return out
    return None  # tables/files are entities


# Virtual attributes every Cypress node has (UI reads them on Attributes tabs).
VIRTUAL_ATTRS = {'opaque_attribute_keys': [], 'user_attributes': {}, 'user_attribute_keys': []}


def cmd_get(params, auth):
    r = resolve(params.get('path'))
    if not r:
        raise CommandError(400, yt_error(500, f'Error resolving path {params.get("path")}',
                                         {'path': params.get('path'), 'code': 500}))
    node, attr_path = r
    if attr_path is not None:
        if attr_path == '':
            return dict(node.attrs)
        head, *rest = attr_path.split('/')
        _MISSING = object()  # mirrors JS `undefined`
        if head in VIRTUAL_ATTRS and head not in node.attrs:
            v = VIRTUAL_ATTRS[head]
        else:
            v = node.attrs.get(head, _MISSING)
        for k in rest:
            if isinstance(v, dict):
                v = v['$value'] if '$value' in v else v
            if isinstance(v, dict):
                v = v.get(k, _MISSING)
            elif isinstance(v, list):  # JS arrays accept string indices
                v = v[int(k)] if k.isdigit() and int(k) < len(v) else _MISSING
        if v is _MISSING:
            raise CommandError(400, yt_error(500, f'Attribute "{head}" is not found', {'code': 500}))
        return v
    value = node_value(node, params)
    attrs = attributes_for(node, params.get('attributes'))
    if attrs:
        return {'$attributes': attrs, '$value': value}
    return value


def cmd_list(params, auth):
    r = resolve(params.get('path'))
    if not r or r[0].kind != 'map_node':
        raise CommandError(400, yt_error(500, f'Error resolving path {params.get("path")}',
                                         {'path': params.get('path'), 'code': 500}))
    out = []
    for name, child in r[0].children.items():
        attrs = attributes_for(child, params.get('attributes'))
        out.append({'$attributes': attrs, '$value': name} if attrs else name)
    return out


def cmd_exists(params, auth):
    return resolve(params.get('path')) is not None


def strip_ranges(p):
    if isinstance(p, dict):
        p = p.get('$value', p)
    return re.sub(r'\[.*\]$', '', str(p))


def range_of(p):
    if isinstance(p, dict) and p.get('$attributes', {}).get('ranges'):
        rng = (p['$attributes']['ranges'] or [{}])[0]
        start = (rng.get('lower_limit') or {}).get('row_index', 0)
        end = (rng.get('upper_limit') or {}).get('row_index')
        return start, (None if end is None else end - start)
    m = re.search(r'\[#(\d+):#(\d+)\]$', str(p))
    if m:
        return int(m.group(1)), int(m.group(2)) - int(m.group(1))
    return 0, None


def cmd_read_table(params, auth):
    r = resolve(strip_ranges(params.get('path')))
    if not r or r[0].kind != 'table':
        raise CommandError(400, yt_error(500, f'Error resolving path {params.get("path")}', {'code': 500}))
    node = r[0]
    start, limit = range_of(params.get('path'))
    of = params.get('output_format')
    of_name = of if isinstance(of, str) else (of or {}).get('$value')
    of_attrs = (of or {}).get('$attributes', {}) if isinstance(of, dict) else {}
    schema = node.attrs['schema']['$value']
    if of_name == 'web_json':

        def _int(v, default):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        return web_json_body(
            schema, node.rows,
            start_row=start,
            row_limit=50 if limit is None else limit,
            # column_names, when present, fully replaces max_selected_column_count.
            column_names=of_attrs.get('column_names'),
            max_selected_column_count=_int(of_attrs.get('max_selected_column_count'), 0) or 50,
            max_all_column_names_count=_int(of_attrs.get('max_all_column_names_count'), 0) or 2000,
        )
    # json format fallback: plain row objects
    return node.rows[start:start + (50 if limit is None else limit)]


def cmd_get_table_columnar_statistics(params, auth):
    return [{'column_data_weights': {}, 'timestamp_total_weight': 0, 'legacy_chunks_data_weight': 0}
            for _ in params.get('paths') or []]


def cmd_whoami(params, auth):
    return {'login': auth['user'], 'realm': 'mock'}


def cmd_check_permission(params, auth):
    return {'action': 'allow', 'object_id': '0-0-0-0', 'object_name': params.get('path'),
            'subject_id': '0-0-0-1', 'subject_name': auth['user']}


def cmd_check_permission_by_acl(params, auth):
    return {'action': 'allow', 'subject_id': '0-0-0-1', 'subject_name': auth['user'],
            'missing_subjects': []}


def cmd_get_supported_features(params, auth):
    return {'features': {'compression_codecs': ['none', 'lz4'], 'erasure_codecs': ['none'],
                         'primitive_types': ['int64', 'uint64', 'double', 'boolean', 'string', 'any']}}


def cmd_execute_batch(params, auth):
    out = []
    for r in params.get('requests') or []:
        impl = COMMANDS.get(r.get('command'))
        if not impl:
            out.append({'error': yt_error(1, f'Command {r.get("command")} is not registered in batch')})
            continue
        try:
            out.append({'output': impl(r.get('parameters') or {}, auth)})
        except CommandError as e:
            out.append({'error': e.err})
        except Exception as e:  # noqa: BLE001 - mirror server.js catch-all
            out.append({'error': yt_error(1, str(e))})
    return out


COMMANDS = {
    'get': cmd_get,
    'list': cmd_list,
    'exists': cmd_exists,
    'read_table': cmd_read_table,
    'get_table_columnar_statistics': cmd_get_table_columnar_statistics,
    'whoami': cmd_whoami,
    'check_permission': cmd_check_permission,
    'check_permission_by_acl': cmd_check_permission_by_acl,
    'get_supported_features': cmd_get_supported_features,
    'execute_batch': cmd_execute_batch,
}

RAW_OUTPUT = {'read_table', 'get_table_columnar_statistics'}

CORS_ALLOW_HEADERS = ', '.join([
    'Content-Type', 'Accept', 'Authorization', 'Origin', 'Referer',
    'X-Csrf-Token', 'X-YT-Parameters', 'X-YT-Parameters-0', 'X-YT-Parameters-1',
    'X-YT-Response-Parameters', 'X-YT-Input-Format', 'X-YT-Output-Format',
    'X-YT-Header-Format', 'X-YT-Suppress-Redirect', 'X-YT-Omit-Trailers',
    'X-YT-Request-Format-Options', 'X-YT-Response-Format-Options',
    'X-YT-Request-Id', 'X-YT-Correlation-Id', 'X-YT-Trace-Id', 'X-YT-User-Tag'])
CORS_EXPOSE_HEADERS = ', '.join([
    'Content-Type', 'X-YT-Error', 'X-YT-Response-Code', 'X-YT-Response-Message',
    'X-YT-Request-Id', 'X-YT-Proxy', 'X-YT-Trace-Id'])


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):  # route through our logger
        pass

    def handle(self):
        if os.environ.get('MOCK_DEBUG_CONN'):
            log('OPEN', self.client_address)
            try:
                super().handle()
            except Exception as e:
                log('CONN EXC', repr(e))
                raise
            finally:
                log('CLOSE', self.client_address, 'close_connection=', self.close_connection)
        else:
            super().handle()

    # ---- helpers ----
    def cors_headers(self):
        origin = self.headers.get('Origin')
        if not origin:
            return {}
        return {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Allow-Methods': 'POST, PUT, GET, OPTIONS',
            'Access-Control-Allow-Headers': CORS_ALLOW_HEADERS,
            'Access-Control-Expose-Headers': CORS_EXPOSE_HEADERS,
            'Access-Control-Max-Age': '3600',
        }

    def send_body(self, status, body_bytes, headers):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body_bytes)))
        # Advertise the connection decision explicitly. An HTTP/1.1 response
        # without a Connection header implies keep-alive to Node clients; if we
        # then close (request said `Connection: close`), a pooled socket dies
        # under the client and later requests fail with "socket hang up".
        self.send_header('Connection', 'close' if self.close_connection else 'keep-alive')
        if not self.close_connection:
            self.send_header('Keep-Alive', 'timeout=5')
            # http.server never times out idle keep-alive sockets; honor the
            # advertised timeout so clients and server agree.
            self.connection.settimeout(5)
        self.end_headers()
        self.wfile.write(body_bytes)
        self._record(status, body_bytes)

    def send_json(self, status, body, extra=None):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_body(status, data, {'Content-Type': 'application/json', **(extra or {})})

    def send_yt_error(self, status, err, extra=None):
        self.send_json(status, err, {
            'X-YT-Error': json.dumps(err, ensure_ascii=False),
            'X-YT-Response-Code': str(err['code']),
            'X-YT-Response-Message': err['message'],
            **(extra or {}),
        })

    def _record(self, status, body_bytes):
        if not RECORD_PATH:
            return
        interesting = ['authorization', 'cookie', 'x-csrf-token', 'content-type', 'accept',
                       'x-yt-correlation-id', 'x-yt-parameters', 'x-yt-input-format',
                       'x-yt-output-format', 'x-yt-header-format', 'x-yt-suppress-redirect']
        req_headers = {h: self.headers[h] for h in interesting if self.headers.get(h)}
        split = urlsplit(self.path)

        def parse(buf):
            if not buf:
                return None
            text = buf.decode('utf-8', 'replace')
            try:
                return json.loads(text)
            except ValueError:
                return text[:4000]

        record({
            'ts': datetime.now(timezone.utc).isoformat(),
            'method': self.command,
            'path': split.path,
            'query': f'?{split.query}' if split.query else '',
            'request_headers': req_headers,
            'request_body': parse(getattr(self, '_body_buf', b'')),
            'status': status,
            'response_body': parse(body_bytes),
        })

    def collect_params(self, query, body_buf):
        """Merge command parameters from query string, X-YT-Parameters header and JSON body."""
        params = dict(parse_qsl(query))
        hdr = self.headers.get('X-YT-Parameters')
        if hdr:
            try:
                params.update(json.loads(hdr))
            except ValueError:
                log(f'  !! failed to parse X-YT-Parameters as JSON: {hdr[:200]}')
        if body_buf:
            ct = self.headers.get('Content-Type') or ''
            if 'json' in ct or not ct:
                try:
                    body = json.loads(body_buf.decode('utf-8'))
                    if isinstance(body, dict):
                        params.update(body)
                except ValueError:
                    pass  # raw data body (e.g. write_table) — ignore
        return params

    def read_request_body(self) -> bytes:
        """Read the request body, supporting both Content-Length and chunked
        Transfer-Encoding. The UI node server streams browser requests through
        axios, which uses chunked encoding — BaseHTTPRequestHandler does not
        decode it, and leftover chunk bytes would corrupt the next request on
        the keep-alive connection (seen as intermittent 504s in the UI)."""
        if (self.headers.get('Transfer-Encoding') or '').lower() == 'chunked':
            chunks = []
            while True:
                size_line = self.rfile.readline(65536).strip()
                size = int(size_line.split(b';')[0] or b'0', 16)
                if size == 0:
                    # consume trailer section up to the final blank line
                    while self.rfile.readline(65536) not in (b'\r\n', b'\n', b''):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF after each chunk
            return b''.join(chunks)
        length = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(length) if length else b''

    # ---- request handling ----
    def handle_request(self):
        split = urlsplit(self.path)
        p = split.path
        cors = self.cors_headers()

        if self.command == 'OPTIONS':
            self.send_body(200, b'', cors)
            return

        body_buf = self.read_request_body()
        self._body_buf = body_buf
        log(self.command, p + (f'?{split.query}' if split.query else ''),
            f'body={body_buf[:300]}' if body_buf else '')

        try:
            self.route(p, split.query, body_buf, cors)
        except Exception as e:  # noqa: BLE001
            log('  !! internal error', repr(e))
            self.send_yt_error(500, yt_error(1, str(e)), cors)
        finally:
            if os.environ.get('MOCK_DEBUG_CONN'):
                log('  reqconn:', self.request_version,
                    'Connection=', self.headers.get('Connection'),
                    'TE=', self.headers.get('Transfer-Encoding'),
                    'close_after=', self.close_connection)

    def route(self, p, query, body_buf, cors):
        # ---- infrastructure endpoints ----
        if p == '/ping':
            return self.send_json(200, {}, cors)
        if p in ('/version', '/service/version'):
            return self.send_body(200, b'mock-proxy-1.0.0', {'Content-Type': 'text/plain', **cors})
        if p == '/hosts/all':
            # System page expects objects here; empty list keeps it from crashing.
            return self.send_json(200, [], cors)
        if p == '/hosts' or p.startswith('/hosts/'):
            return self.send_json(200, [HOST], cors)
        if p in ('/api', '/api/'):
            return self.send_json(200, ['v3', 'v4'], cors)

        # ---- password login: HTTP Basic auth to /login, per cypress_cookie_login.cpp.
        # Real proxy replies with empty 200 + Set-Cookie: YTCypressCookie (no SameSite).
        if p == '/login':
            m = re.match(r'^Basic (.+)$', self.headers.get('Authorization') or '')
            user, password = '', ''
            if m:
                decoded = base64.b64decode(m.group(1)).decode('utf-8', 'replace')
                user, _, password = decoded.partition(':')
            if user not in users or users[user]['password'] != password:
                return self.send_yt_error(401, yt_error(900, 'Invalid username or password'), cors)
            cookie = make_cookie(user)
            expires = format_datetime(datetime.now(timezone.utc) + timedelta(days=30), usegmt=True)
            return self.send_body(200, b'', {
                **cors,
                'Set-Cookie': f'YTCypressCookie={cookie}; Expires={expires}; HttpOnly; Path=/',
            })

        # ---- /auth/whoami: the single auth gate the UI server checks on every request.
        # Must succeed with a truthy csrf_token even without credentials (auth "none" mode).
        if p == '/auth/whoami':
            auth = authenticate(self.headers)
            user = auth['user']
            return self.send_json(200, {
                'login': user,
                'realm': 'cypress_cookie' if auth['via_cookie'] else 'mock',
                'real_login': user,
                'csrf_token': csrf_token_for(user),
            }, cors)

        # ---- API commands ----
        m = re.match(r'^/api/(v3|v4)/(\w+)$', p)
        if m:
            version, command = m.groups()
            auth = authenticate(self.headers)
            if not auth:
                return self.send_yt_error(401, yt_error(900, 'Authentication failed'), cors)
            if not check_csrf(self.command, self.headers, auth):
                return self.send_yt_error(401, yt_error(901, 'CSRF token mismatch'), cors)
            params = self.collect_params(query, body_buf)
            impl = COMMANDS.get(command)
            if not impl:
                log(f'  !! unimplemented command: {command} params={json.dumps(params)[:500]}')
                return self.send_yt_error(404, yt_error(1, f'Command {command} is not registered'), cors)
            try:
                result = impl(params, auth)
            except CommandError as e:
                return self.send_yt_error(e.status, e.err, cors)
            # The request's output_format governs the envelope: with annotate_with_types
            # every scalar in the result (including batch sub-results) is {$type,$value}.
            of = params.get('output_format')
            typed = bool(isinstance(of, dict) and of.get('$attributes', {}).get('annotate_with_types'))
            if command in RAW_OUTPUT:
                payload = result
            else:
                payload = typed_annotate(result) if typed else annotated(result)
            if version == 'v4' and command in ('get', 'list', 'exists'):
                payload = {'value': payload}
            return self.send_json(200, payload, {**cors, 'X-YT-Proxy': HOST})

        log('  !! unhandled route')
        self.send_yt_error(404, yt_error(1, f'No such route: {p}'), cors)

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = handle_request


class DualStackServer(ThreadingHTTPServer):
    """Bind IPv6 with V6ONLY off so both ::1 and 127.0.0.1 connect (Node's
    server.listen() is dual-stack; axios may resolve localhost to ::1 first).

    request_queue_size: the http.server default listen backlog is 5; the UI
    node server opens ~20 parallel non-keep-alive connections per page load,
    overflowing it and producing intermittent connection resets (504s in the
    UI). Node's default backlog is 511 — match it.
    """
    address_family = socket.AF_INET6
    request_queue_size = 511

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


if __name__ == '__main__':
    server = DualStackServer(('', PORT), Handler)
    log(f'mock YT proxy (python) listening on http://{HOST}')
    server.serve_forever()
