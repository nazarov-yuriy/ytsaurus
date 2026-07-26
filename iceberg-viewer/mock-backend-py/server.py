#!/usr/bin/env python3
"""Mock YTsaurus HTTP proxy serving in-RAM fake data to ytsaurus-ui.

Run: python3 server.py [port]   (default 8000)
Port of ../mock-backend/server.js; parity checked by recordings/replay-diff.py.
Set MOCK_RECORD=<path> to append request/response pairs as JSONL.
"""
import base64
import binascii
import json
import os
import re
import secrets
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import userdb
from data import resolve
from webjson import annotated, typed_annotate, web_json_body

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
HOST = os.environ.get('MOCK_HOST', f'localhost:{PORT}')
RECORD_PATH = os.environ.get('MOCK_RECORD')
REQUIRE_AUTH = bool(os.environ.get('MOCK_REQUIRE_AUTH'))
ROBOT_TOKEN = os.environ.get('MOCK_ROBOT_TOKEN', '')

# MOCK_DELAY simulates a slow catalog: "1500" delays every data command by 1.5s,
# "read_table:5000,list:2000" per command. //sys paths are never delayed — the
# UI server's boot-path robot requests have a 5s timeout (see docs/timeouts.md).
DELAYS = {}
for _part in filter(None, os.environ.get('MOCK_DELAY', '').split(',')):
    _cmd, _, _ms = _part.partition(':')
    if _ms:
        DELAYS[_cmd] = int(_ms)
    else:
        DELAYS.update(dict.fromkeys(('get', 'list', 'exists', 'read_table'), int(_cmd)))


def maybe_delay(command, params):
    ms = DELAYS.get(command, 0)
    if ms and not str((params or {}).get('path', '')).startswith('//sys'):
        time.sleep(ms / 1000)


def log(*args):
    print(datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3], *args, flush=True)


def yt_error(code, message, attributes=None):
    return {'code': code, 'message': message, 'attributes': attributes or {}, 'inner_errors': []}


class CommandError(Exception):
    def __init__(self, status, err):
        self.status, self.err = status, err


def resolve_error(path):
    return CommandError(400, yt_error(500, f'Error resolving path {path}', {'path': path, 'code': 500}))


# ---- auth (user/session storage lives in userdb: PostgreSQL or in-RAM) -----

def csrf_token_for(user):
    return f'csrf-{user}'


def authenticate(headers):
    cookies = dict(p.strip().split('=', 1) for p in (headers.get('Cookie') or '').split(';') if '=' in p)
    cookie = cookies.get('YTCypressCookie')
    if cookie and (user := userdb.session_user(cookie)):
        return {'user': user, 'via_cookie': True}
    token = (headers.get('Authorization') or '').removeprefix('OAuth ').strip()
    if headers.get('Authorization', '').startswith('OAuth ') and token:
        if REQUIRE_AUTH:
            if ROBOT_TOKEN and secrets.compare_digest(token, ROBOT_TOKEN):
                return {'user': 'iceberg', 'via_cookie': False}
            return None
        return {'user': token if userdb.user_exists(token) else 'iceberg', 'via_cookie': False}
    if REQUIRE_AUTH:
        return None
    return {'user': 'iceberg', 'via_cookie': False}  # auth "none" sends no credentials


def check_csrf(method, headers, auth):
    if not auth['via_cookie'] or method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    return headers.get('X-Csrf-Token') == csrf_token_for(auth['user'])


# ---- commands --------------------------------------------------------------

VIRTUAL_ATTRS = {'opaque_attribute_keys': [], 'user_attributes': {}, 'user_attribute_keys': []}
_MISSING = object()


def attributes_for(node, requested):
    keys = requested if isinstance(requested, list) else (requested or {}).get('keys', [])
    return {k: node.attrs[k] for k in keys if node.attrs.get(k) is not None}


def cmd_get(params, auth):
    r = resolve(params.get('path'))
    if not r:
        raise resolve_error(params.get('path'))
    node, attr_path = r
    if attr_path is None:
        if node.kind == 'map_node':
            value = {name: {'$attributes': attributes_for(c, params.get('attributes')),
                            '$value': {} if c.kind == 'map_node' else None}
                     for name, c in node.children.items()}
        else:
            value = None
        attrs = attributes_for(node, params.get('attributes'))
        return {'$attributes': attrs, '$value': value} if attrs else value
    if attr_path == '':
        return dict(node.attrs)
    head, *rest = attr_path.split('/')
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


def cmd_list(params, auth):
    r = resolve(params.get('path'))
    if not r or r[0].kind != 'map_node':
        raise resolve_error(params.get('path'))
    out = []
    for name, child in r[0].children.items():
        attrs = attributes_for(child, params.get('attributes'))
        out.append({'$attributes': attrs, '$value': name} if attrs else name)
    return out


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
    return (int(m.group(1)), int(m.group(2)) - int(m.group(1))) if m else (0, None)


def cmd_read_table(params, auth):
    path = params.get('path')
    r = resolve(strip_ranges(path))
    if not r or r[0].kind != 'table':
        raise CommandError(400, yt_error(500, f'Error resolving path {path}', {'code': 500}))
    node = r[0]
    start, limit = range_of(path)
    of = params.get('output_format')
    of_attrs = of.get('$attributes', {}) if isinstance(of, dict) else {}
    schema = node.attrs['schema']['$value']
    if (of if isinstance(of, str) else (of or {}).get('$value')) == 'web_json':
        def _int(v, default):
            try:
                return int(v) or default
            except (TypeError, ValueError):
                return default
        return web_json_body(
            schema, node.rows, start_row=start, row_limit=50 if limit is None else limit,
            column_names=of_attrs.get('column_names'),
            max_selected_column_count=_int(of_attrs.get('max_selected_column_count'), 50),
            max_all_column_names_count=_int(of_attrs.get('max_all_column_names_count'), 2000),
            value_format=of_attrs.get('value_format'))
    return node.rows[start:start + (50 if limit is None else limit)]


def cmd_execute_batch(params, auth):
    out = []
    for r in params.get('requests') or []:
        impl = COMMANDS.get(r.get('command'))
        if not impl:
            out.append({'error': yt_error(1, f'Command {r.get("command")} is not registered in batch')})
            continue
        try:
            maybe_delay(r.get('command'), r.get('parameters'))
            out.append({'output': impl(r.get('parameters') or {}, auth)})
        except CommandError as e:
            out.append({'error': e.err})
        except Exception as e:
            out.append({'error': yt_error(1, str(e))})
    return out


COMMANDS = {
    'get': cmd_get,
    'list': cmd_list,
    'exists': lambda p, a: resolve(p.get('path')) is not None,
    'read_table': cmd_read_table,
    'execute_batch': cmd_execute_batch,
    'whoami': lambda p, a: {'login': a['user'], 'realm': 'mock'},
    'check_permission': lambda p, a: {'action': 'allow', 'object_id': '0-0-0-0',
                                      'object_name': p.get('path'), 'subject_id': '0-0-0-1',
                                      'subject_name': a['user']},
    'check_permission_by_acl': lambda p, a: {'action': 'allow', 'subject_id': '0-0-0-1',
                                             'subject_name': a['user'], 'missing_subjects': []},
    'get_supported_features': lambda p, a: {'features': {
        'compression_codecs': ['none', 'lz4'], 'erasure_codecs': ['none'],
        'primitive_types': ['int64', 'uint64', 'double', 'boolean', 'string', 'any']}},
    'get_table_columnar_statistics': lambda p, a: [
        {'column_data_weights': {}, 'timestamp_total_weight': 0, 'legacy_chunks_data_weight': 0}
        for _ in p.get('paths') or []],
}

RAW_OUTPUT = {'read_table', 'get_table_columnar_statistics'}

CORS_ALLOW = ('Content-Type, Accept, Authorization, Origin, Referer, X-Csrf-Token, '
              'X-YT-Parameters, X-YT-Parameters-0, X-YT-Parameters-1, X-YT-Response-Parameters, '
              'X-YT-Input-Format, X-YT-Output-Format, X-YT-Header-Format, X-YT-Suppress-Redirect, '
              'X-YT-Omit-Trailers, X-YT-Request-Format-Options, X-YT-Response-Format-Options, '
              'X-YT-Request-Id, X-YT-Correlation-Id, X-YT-Trace-Id, X-YT-User-Tag')
CORS_EXPOSE = ('Content-Type, X-YT-Error, X-YT-Response-Code, X-YT-Response-Message, '
               'X-YT-Request-Id, X-YT-Proxy, X-YT-Trace-Id')
RECORDED_HEADERS = ('authorization', 'cookie', 'x-csrf-token', 'content-type', 'accept',
                    'x-yt-correlation-id', 'x-yt-parameters', 'x-yt-input-format',
                    'x-yt-output-format', 'x-yt-header-format', 'x-yt-suppress-redirect')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def cors_headers(self):
        origin = self.headers.get('Origin')
        if not origin:
            return {}
        return {'Access-Control-Allow-Origin': origin,
                'Access-Control-Allow-Credentials': 'true',
                'Access-Control-Allow-Methods': 'POST, PUT, GET, OPTIONS',
                'Access-Control-Allow-Headers': CORS_ALLOW,
                'Access-Control-Expose-Headers': CORS_EXPOSE,
                'Access-Control-Max-Age': '3600'}

    def send_body(self, status, body_bytes, headers):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body_bytes)))
        # Advertise the close decision: Node clients treat a header-less HTTP/1.1
        # response as keep-alive and would pool a socket we are about to close.
        self.send_header('Connection', 'close' if self.close_connection else 'keep-alive')
        if not self.close_connection:
            self.send_header('Keep-Alive', 'timeout=5')
            self.connection.settimeout(5)
        self.end_headers()
        self.wfile.write(body_bytes)
        self.record(status, body_bytes)

    def send_json(self, status, body, extra=None):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_body(status, data, {'Content-Type': 'application/json', **(extra or {})})

    def send_yt_error(self, status, err, extra=None):
        self.send_json(status, err, {'X-YT-Error': json.dumps(err, ensure_ascii=False),
                                     'X-YT-Response-Code': str(err['code']),
                                     'X-YT-Response-Message': err['message'], **(extra or {})})

    def record(self, status, body_bytes):
        if not RECORD_PATH:
            return

        def parse(buf):
            if not buf:
                return None
            try:
                return json.loads(buf)
            except ValueError:
                return buf.decode('utf-8', 'replace')[:4000]

        split = urlsplit(self.path)
        with open(RECORD_PATH, 'a') as f:
            f.write(json.dumps({
                'ts': datetime.now(timezone.utc).isoformat(),
                'method': self.command, 'path': split.path,
                'query': f'?{split.query}' if split.query else '',
                'request_headers': {h: self.headers[h] for h in RECORDED_HEADERS if self.headers.get(h)},
                'request_body': parse(self.body_buf), 'status': status,
                'response_body': parse(body_bytes)}, ensure_ascii=False) + '\n')

    def read_request_body(self):
        # axios streams proxied requests as chunked; http.server does not decode it.
        if (self.headers.get('Transfer-Encoding') or '').lower() == 'chunked':
            chunks = []
            while (size := int(self.rfile.readline(65536).strip().split(b';')[0] or b'0', 16)):
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            while self.rfile.readline(65536) not in (b'\r\n', b'\n', b''):
                pass
            return b''.join(chunks)
        return self.rfile.read(int(self.headers.get('Content-Length') or 0))

    def collect_params(self, query, body_buf):
        params = dict(parse_qsl(query))
        if hdr := self.headers.get('X-YT-Parameters'):
            try:
                params.update(json.loads(hdr))
            except ValueError:
                log(f'  !! bad X-YT-Parameters: {hdr[:200]}')
        if body_buf and ('json' in (self.headers.get('Content-Type') or 'json')):
            try:
                body = json.loads(body_buf)
                if isinstance(body, dict):
                    params.update(body)
            except ValueError:
                pass  # raw data body
        return params

    def handle_request(self):
        split = urlsplit(self.path)
        cors = self.cors_headers()
        self.body_buf = b''
        if self.command == 'OPTIONS':
            return self.send_body(200, b'', cors)
        self.body_buf = self.read_request_body()
        log(self.command, split.path + (f'?{split.query}' if split.query else ''),
            f'body={self.body_buf[:300]}' if self.body_buf else '')
        try:
            self.route(split.path, split.query, cors)
        except Exception as e:
            log('  !! internal error', repr(e))
            self.send_yt_error(500, yt_error(1, str(e)), cors)

    def route(self, p, query, cors):
        if p == '/ping':
            return self.send_json(200, {}, cors)
        if p == '/ready':
            return self.send_json(200 if userdb.healthy() else 503, {}, cors)
        if p in ('/version', '/service/version'):
            return self.send_body(200, b'mock-proxy-1.0.0', {'Content-Type': 'text/plain', **cors})
        if p == '/hosts/all':
            return self.send_json(200, [], cors)
        if p == '/hosts' or p.startswith('/hosts/'):
            return self.send_json(200, [HOST], cors)
        if p in ('/api', '/api/'):
            return self.send_json(200, ['v3', 'v4'], cors)

        if p == '/login':  # HTTP Basic; real proxy sets YTCypressCookie, no SameSite
            authorization = self.headers.get('Authorization')
            if authorization is None:
                return self.send_body(
                    401, b'', {**cors, 'WWW-Authenticate': 'Basic'})
            if ' ' not in authorization:
                return self.send_yt_error(400, yt_error(
                    1, 'Malformed "Authorization" header: failed to parse authorization method'),
                    cors)
            method, encoded_credentials = authorization.split(' ', 1)
            if method != 'Basic':
                return self.send_yt_error(
                    400, yt_error(1, f'Unsupported authorization method "{method}"'), cors)
            try:
                credentials = base64.b64decode(encoded_credentials, validate=True)
            except (binascii.Error, ValueError):
                return self.send_yt_error(
                    400, yt_error(1, 'Failed to decode user credentials'), cors)
            if b':' not in credentials:
                return self.send_yt_error(
                    400, yt_error(1, 'Failed to parse user credentials'), cors)
            user_bytes, password_bytes = credentials.split(b':', 1)
            user = user_bytes.decode('utf-8', 'replace')
            password = password_bytes.decode('utf-8', 'replace')
            if not userdb.verify(user, password):
                # Real proxy masks the cause: generic code 1 (cypress_cookie_login.cpp:83).
                return self.send_yt_error(
                    401, yt_error(1, 'Incorrect login or password'),
                    {**cors, 'WWW-Authenticate': 'Basic'})
            cookie = userdb.create_session(user)
            expires = format_datetime(datetime.now(timezone.utc) + timedelta(days=30), usegmt=True)
            return self.send_body(200, b'', {
                **cors, 'Set-Cookie': f'YTCypressCookie={cookie}; Expires={expires}; HttpOnly; Path=/'})

        if p == '/auth/whoami':  # must succeed with truthy csrf_token even without credentials
            auth = authenticate(self.headers)
            if not auth:
                return self.send_yt_error(401, yt_error(900, 'Authentication failed'), cors)
            return self.send_json(200, {
                'login': auth['user'],
                'realm': 'cypress_cookie' if auth['via_cookie'] else 'mock',
                'real_login': auth['user'], 'csrf_token': csrf_token_for(auth['user'])}, cors)

        if m := re.match(r'^/api/(v3|v4)/(\w+)$', p):
            version, command = m.groups()
            auth = authenticate(self.headers)
            if not auth:
                return self.send_yt_error(401, yt_error(900, 'Authentication failed'), cors)
            if not check_csrf(self.command, self.headers, auth):
                return self.send_yt_error(401, yt_error(901, 'CSRF token mismatch'), cors)
            params = self.collect_params(query, self.body_buf)
            impl = COMMANDS.get(command)
            if not impl:
                log(f'  !! unimplemented command: {command}')
                return self.send_yt_error(404, yt_error(1, f'Command {command} is not registered'), cors)
            try:
                maybe_delay(command, params)
                result = impl(params, auth)
            except CommandError as e:
                return self.send_yt_error(e.status, e.err, cors)
            of = params.get('output_format')
            typed = isinstance(of, dict) and of.get('$attributes', {}).get('annotate_with_types')
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
    # Dual-stack bind (Node parity); backlog 511: a page load bursts ~20 connections.
    address_family = socket.AF_INET6
    request_queue_size = 511

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


if __name__ == '__main__':
    log(f'mock YT proxy (python) listening on http://{HOST}')
    DualStackServer(('', PORT), Handler).serve_forever()
