#!/usr/bin/env python3
"""Mock YTsaurus HTTP proxy serving in-RAM fake data to ytsaurus-ui.

Run: python3 server.py [port]   (default 8000)
This is the sole mock-backend implementation: protocol logic on a
FastAPI/uvicorn HTTP layer (see requirements.txt for the pinned versions).
Set MOCK_RECORD=<path> to append request/response pairs as JSONL.
"""
import asyncio
import base64
import binascii
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from http.cookies import CookieError, SimpleCookie

import uvicorn
from fastapi import FastAPI, Request, Response

import userdb
from data import resolve
from webjson import annotated, typed_annotate, web_json_body, yson_text

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
HOST = os.environ.get('MOCK_HOST', f'localhost:{PORT}')
BIND_HOST = os.environ.get('MOCK_BIND_HOST', '127.0.0.1')
RECORD_PATH = os.environ.get('MOCK_RECORD')
REQUIRE_AUTH = bool(os.environ.get('MOCK_REQUIRE_AUTH'))
ROBOT_TOKEN = os.environ.get('MOCK_ROBOT_TOKEN', '')
# Real YTsaurus HTTP proxy that verifies identity for users not added locally,
# e.g. http://proxy.yt.svc:80 — see docs/auth.md "External authentication".
UPSTREAM = os.environ.get('MOCK_YT_UPSTREAM', '').rstrip('/')
UPSTREAM_TIMEOUT = float(os.environ.get('MOCK_YT_UPSTREAM_TIMEOUT') or 5)
if RECORD_PATH and (REQUIRE_AUTH or UPSTREAM):
    raise RuntimeError(
        'MOCK_RECORD is a development-only fixture and cannot be used with '
        'authenticated or delegated authentication')
if UPSTREAM and not REQUIRE_AUTH:
    raise RuntimeError(
        'MOCK_YT_UPSTREAM requires MOCK_REQUIRE_AUTH=1; refusing to start '
        'with delegated verification behind anonymous fallback')
if REQUIRE_AUTH and ROBOT_TOKEN == 'mock-robot-token':
    raise RuntimeError(
        'MOCK_ROBOT_TOKEN must be changed from the published '
        'mock-robot-token placeholder in authenticated mode')


def _cors_origins(raw_value):
    origins = set()
    for origin in filter(None, (part.strip() for part in raw_value.split(','))):
        parsed = urllib.parse.urlsplit(origin)
        if (
                origin == 'null'
                or parsed.scheme not in ('http', 'https')
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment):
            raise RuntimeError(
                'MOCK_CORS_ORIGINS entries must be exact http(s) origins '
                'without credentials, paths, queries, or fragments')
        origins.add(origin)
    return frozenset(origins)


CORS_ORIGINS = _cors_origins(os.environ.get('MOCK_CORS_ORIGINS', ''))

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


def gather_header(headers, name):
    """Gather a direct YT header or its base64-encoded numbered parts."""
    if (value := headers.get(name)) is not None:
        return value
    parts = []
    for index in range(1001):
        value = headers.get(f'{name}{index}')
        if value is None:
            value = headers.get(f'{name}-{index}')
        if value is None:
            break
        if index == 1000:
            raise ValueError(f'Too many {name} header parts')
        parts.append(value)
    if not parts:
        return None
    try:
        return base64.b64decode(''.join(parts), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as error:
        raise ValueError(f'Unable to parse {name} header') from error


def parse_error_format(headers):
    """Parse the structured X-YT-Error-Format header used by the real proxy."""
    def validate(name, annotate_with_types):
        if name not in ('json', 'web_json', 'yson'):
            raise ValueError(f'Unsupported X-YT-Error-Format: {name}')
        return (name, annotate_with_types)

    raw = gather_header(headers, 'X-YT-Error-Format')
    if raw is None:
        return ('json', False)

    header_format = (headers.get('X-YT-Header-Format') or 'json').strip()
    header_format_name = header_format.rsplit('>', 1)[-1].strip().strip('"')
    if header_format_name == 'yson':
        match = re.fullmatch(r'(?:<(?P<attrs>[^>]*)>)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)',
                             raw.strip())
        if not match:
            raise ValueError('Unable to parse X-YT-Error-Format header')
        attrs = match.group('attrs') or ''
        return validate(match.group('name'), bool(re.search(
            r'(?:^|[; ])annotate_with_types\s*=\s*%true(?:[; ]|$)', attrs)))

    if header_format_name != 'json':
        raise ValueError('Unsupported X-YT-Header-Format')
    try:
        node = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError('Unable to parse X-YT-Error-Format header') from error
    if isinstance(node, str):
        return validate(node, False)
    if isinstance(node, dict) and isinstance(node.get('$value'), str):
        attrs = node.get('$attributes') or {}
        return validate(node['$value'], attrs.get('annotate_with_types') is True)
    raise ValueError('Unable to parse X-YT-Error-Format header')


def format_error_header(err, error_format):
    name, annotate_with_types = error_format
    if name == 'yson':
        return yson_text(err), 'application/x-yt-yson-text'
    if name in ('json', 'web_json'):
        obj = typed_annotate(err) if annotate_with_types else err
        return json.dumps(obj, ensure_ascii=True), 'application/json'
    raise ValueError(f'Unsupported X-YT-Error-Format: {name}')


def escape_header_value(value):
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


# ---- auth (user/session storage lives in userdb: PostgreSQL or in-RAM) -----

class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward login credentials to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


UPSTREAM_OPENER = urllib.request.build_opener(RefuseRedirects())


def has_cypress_cookie(headers):
    for header in headers.get_all('Set-Cookie', ()):
        cookies = SimpleCookie()
        try:
            cookies.load(header)
        except CookieError:
            continue
        cookie = cookies.get('YTCypressCookie')
        if cookie is not None and cookie.value:
            return True
    return False


def upstream_login(encoded_credentials):
    """Verify Basic credentials against the real YTsaurus /login.

    True = accepted, False = rejected, None = upstream unavailable.
    """
    req = urllib.request.Request(
        f'{UPSTREAM}/login', method='POST',
        headers={'Authorization': f'Basic {encoded_credentials}'})
    try:
        with UPSTREAM_OPENER.open(req, timeout=UPSTREAM_TIMEOUT) as response:
            if 200 <= response.status < 300 and has_cypress_cookie(response.headers):
                return True
            return None
    except urllib.error.HTTPError as e:
        return False if 400 <= e.code < 500 else None
    except OSError:
        return None


CSRF_TTL = int(os.environ.get('MOCK_CSRF_TTL_SECONDS') or 24 * 3600)


def csrf_token_for(user):
    # Real construction (auth_server/helpers.cpp SignCsrfToken):
    # hex(hmac_sha256(secret, "user:unix_ts")) + ":" + unix_ts
    ts = int(time.time())
    sig = hmac.new(userdb.csrf_secret().encode(), f'{user}:{ts}'.encode(), 'sha256').hexdigest()
    return f'{sig}:{ts}'


def check_csrf_token(token, user):
    """Match YTsaurus CheckCsrfToken; return (HTTP status, YT error) or None."""
    parts = token.strip().split(':')
    if len(parts) != 2 or not parts[1].isdigit():
        return 503, yt_error(1, 'Malformed CSRF token')
    sig, ts = parts
    try:
        sign_time = int(ts)
    except ValueError:
        return 503, yt_error(1, 'Malformed CSRF token')
    if sign_time > 2 ** 53 - 1:
        return 503, yt_error(1, 'Malformed CSRF token')
    if sign_time < time.time() - CSRF_TTL:
        return 401, yt_error(110, 'CSRF token expired')
    expected = hmac.new(userdb.csrf_secret().encode(), f'{user}:{ts}'.encode(), 'sha256').hexdigest()
    if not hmac.compare_digest(expected, sig):
        # Typo preserved from auth_server/helpers.cpp:187.
        return 401, yt_error(110, 'Invalid CSFR token signature')
    return None


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
    """Returns (HTTP status, YT error), or None when the request passes."""
    if not auth['via_cookie'] or method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    token = headers.get('X-Csrf-Token')
    if token is None:
        return 401, yt_error(111, 'CSRF token is missing')
    return check_csrf_token(token, auth['user'])


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
    # Empty-but-valid answers so the Operations/Queries pages render empty states
    # instead of error blocks (shapes per scheduler_commands.cpp:417-441 and
    # query_commands.cpp:429-437).
    'list_operations': lambda p, a: {
        'operations': [], 'incomplete': False, 'pool_tree_counts': {}, 'pool_counts': {},
        'user_counts': {}, 'state_counts': {}, 'type_counts': {}, 'failed_jobs_count': 0},
    'get_query_tracker_info': lambda p, a: {
        'query_tracker_stage': 'production', 'cluster_name': 'mock',
        'supported_features': {}, 'access_control_objects': [], 'clusters': [],
        'engines_info': {}},
}

RAW_OUTPUT = {'read_table', 'get_table_columnar_statistics'}

CORS_ALLOW = ('Content-Type, Accept, Authorization, Origin, Referer, X-Csrf-Token, '
              'X-YT-Parameters, X-YT-Parameters-0, X-YT-Parameters-1, X-YT-Response-Parameters, '
              'X-YT-Input-Format, X-YT-Output-Format, X-YT-Error-Format, X-YT-Header-Format, '
              'X-YT-Suppress-Redirect, '
              'X-YT-Omit-Trailers, X-YT-Request-Format-Options, X-YT-Response-Format-Options, '
              'X-YT-Request-Id, X-YT-Correlation-Id, X-YT-Trace-Id, X-YT-User-Tag')
CORS_EXPOSE = ('Content-Type, X-YT-Error, X-YT-Response-Code, X-YT-Response-Message, '
               'X-YT-Request-Id, X-YT-Proxy, X-YT-Trace-Id')
RECORDED_HEADERS = ('authorization', 'cookie', 'x-csrf-token', 'content-type', 'accept',
                    'x-yt-correlation-id', 'x-yt-parameters', 'x-yt-input-format',
                    'x-yt-output-format', 'x-yt-header-format', 'x-yt-suppress-redirect')
RECORD_FILE_LIMIT_BYTES = 50 * 1024 * 1024
RECORD_BODY_LIMIT_BYTES = 64 * 1024
RECORD_REDACTED = '<redacted>'
_RECORD_SECRET_KEYS = frozenset({
    'access_token', 'api_key', 'authorization', 'authorization_code',
    'client_secret', 'cookie', 'credential', 'csrf_token', 'id_token',
    'password', 'passwd', 'private_key', 'proxy_authorization',
    'refresh_token', 'robot_token', 'secret', 'session', 'session_token',
    'set_cookie', 'x_csrf_token', 'ytcypresscookie',
})
_RECORD_SECRET_SUFFIXES = (
    '_credential', '_credentials', '_password', '_passwd', '_secret', '_token')
_REDACTED_RECORD_HEADERS = frozenset({
    'authorization', 'cookie', 'x-csrf-token', 'x-yt-parameters'})
_AUTHORIZATION_VALUE = re.compile(
    r'(?i)\b(Basic|Bearer|OAuth)(\s+)[A-Za-z0-9._~+/=-]+')
_INLINE_SECRET_VALUE = re.compile(
    r'(?i)\b(access_token|api_key|authorization_code|client_secret|csrf_token|'
    r'id_token|password|passwd|refresh_token|robot_token|session_token)'
    r'(\s*[=:]\s*)([^&\s,;]+)')
_RECORD_LOCK = threading.Lock()
_record_limit_reported = False
_record_error_reported = False
# Infrastructure endpoints (health probes, discovery) are not user actions.
AUDIT_EXEMPT = ('/ping', '/ready', '/version', '/service/version', '/api', '/api/', '/hosts')


# ---- HTTP layer (FastAPI/uvicorn) ------------------------------------------

app = FastAPI(openapi_url=None)  # the YT proxy protocol is not REST; no docs
METHODS = ['GET', 'POST', 'PUT', 'DELETE']
# Neither audit persistence nor PostgreSQL health checks may occupy FastAPI's
# request-handler pool. The semaphores ensure the executors never accumulate
# their own unbounded work queues.
_AUDIT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mock-audit')
_AUDIT_CAPACITY = asyncio.Semaphore(1)
_AUDIT_TIMEOUT_SECONDS = 5
_HEALTH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mock-health')
_HEALTH_CAPACITY = asyncio.Semaphore(1)
_HEALTH_TIMEOUT_SECONDS = float(
    os.environ.get('MOCK_HEALTH_TIMEOUT_SECONDS') or 0.5)
if _HEALTH_TIMEOUT_SECONDS <= 0:
    raise RuntimeError('MOCK_HEALTH_TIMEOUT_SECONDS must be positive')
_INFRA_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mock-infra')
_INFRA_CAPACITY = asyncio.Semaphore(2)


async def run_dedicated(executor, capacity, function, *args):
    await capacity.acquire()
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(executor, function, *args)
    except BaseException:
        capacity.release()
        raise
    future.add_done_callback(lambda _future: capacity.release())
    return await asyncio.shield(future)


async def run_audit(*args):
    """Run one audit with bounded admission while preserving fail-open serving."""
    try:
        await asyncio.wait_for(
            _AUDIT_CAPACITY.acquire(), timeout=_AUDIT_TIMEOUT_SECONDS)
    except TimeoutError as error:
        raise TimeoutError('audit executor remained busy') from error
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(_AUDIT_EXECUTOR, userdb.audit, *args)
    except BaseException:
        _AUDIT_CAPACITY.release()
        raise
    # A request cancellation or timeout must not admit more work while this
    # dedicated worker is still occupied.
    future.add_done_callback(lambda _future: _AUDIT_CAPACITY.release())
    try:
        return await asyncio.wait_for(
            asyncio.shield(future), timeout=_AUDIT_TIMEOUT_SECONDS)
    except TimeoutError as error:
        raise TimeoutError('audit write timed out') from error


def cors_headers(request):
    origin = request.headers.get('Origin')
    if not origin or origin not in CORS_ORIGINS:
        return {}
    return {'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Allow-Methods': 'POST, PUT, GET, OPTIONS',
            'Access-Control-Allow-Headers': CORS_ALLOW,
            'Access-Control-Expose-Headers': CORS_EXPOSE,
            'Access-Control-Max-Age': '3600',
            'Vary': 'Origin'}


def _record_key_is_sensitive(key):
    words = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', str(key))
    normalized = re.sub(r'[^a-z0-9]+', '_', words.lower()).strip('_')
    return (
        normalized in _RECORD_SECRET_KEYS
        or normalized.endswith(_RECORD_SECRET_SUFFIXES))


def _redact_record_text(value):
    value = _AUTHORIZATION_VALUE.sub(
        lambda match: f'{match.group(1)}{match.group(2)}{RECORD_REDACTED}',
        value)
    return _INLINE_SECRET_VALUE.sub(
        lambda match: (
            f'{match.group(1)}{match.group(2)}{RECORD_REDACTED}'),
        value)


def _redact_record_value(value, depth=0):
    if depth >= 12 and isinstance(value, (dict, list)):
        return '<omitted: nesting too deep>'
    if isinstance(value, dict):
        return {
            str(key): (
                RECORD_REDACTED if _record_key_is_sensitive(key)
                else _redact_record_value(child, depth + 1))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_record_value(child, depth + 1) for child in value]
    if isinstance(value, str):
        return _redact_record_text(value)
    return value


def _record_body(buf):
    if not buf:
        return None
    if len(buf) > RECORD_BODY_LIMIT_BYTES:
        return {
            '_recording_omitted': 'body exceeds recording limit',
            'byte_length': len(buf),
        }
    try:
        value = json.loads(buf)
    except (TypeError, ValueError):
        return {
            '_recording_omitted': 'non-JSON body',
            'byte_length': len(buf),
        }
    return _redact_record_value(value)


def _record_query(raw_query):
    if not raw_query:
        return ''
    if len(raw_query.encode('utf-8', 'replace')) > RECORD_BODY_LIMIT_BYTES:
        return '?_recording_omitted=query+exceeds+recording+limit'
    try:
        fields = urllib.parse.parse_qsl(
            raw_query, keep_blank_values=True, max_num_fields=1000)
    except ValueError:
        return '?_recording_omitted=too+many+query+fields'
    safe_fields = [
        (key, RECORD_REDACTED if (
            _record_key_is_sensitive(key) or key.lower() in ('code', 'state'))
         else _redact_record_text(value))
        for key, value in fields
    ]
    return '?' + urllib.parse.urlencode(safe_fields)


def _record_headers(headers):
    result = {}
    for header in RECORDED_HEADERS:
        value = headers.get(header)
        if not value:
            continue
        result[header] = (
            RECORD_REDACTED if header in _REDACTED_RECORD_HEADERS
            else _redact_record_text(value))
    return result


def record(request, status, body_bytes):
    global _record_error_reported, _record_limit_reported
    if not RECORD_PATH:
        return

    entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'method': request.method,
        'path': request.url.path,
        'query': _record_query(request.url.query),
        'request_headers': _record_headers(request.headers),
        'request_body': _record_body(request.state.body_buf),
        'status': status,
        'response_body': _record_body(body_bytes),
    }
    encoded = (
        json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n'
    ).encode('utf-8')
    try:
        with _RECORD_LOCK:
            descriptor = os.open(
                RECORD_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, 'ab') as output:
                output.seek(0, os.SEEK_END)
                if output.tell() + len(encoded) > RECORD_FILE_LIMIT_BYTES:
                    if not _record_limit_reported:
                        log('  !! recording size limit reached; further entries omitted')
                        _record_limit_reported = True
                    return
                output.write(encoded)
    except OSError as error:
        if not _record_error_reported:
            log('  !! recording write failed', type(error).__name__)
            _record_error_reported = True


def respond(request, status, body_bytes, headers=None, media_type=None):
    record(request, status, body_bytes)
    return Response(body_bytes, status_code=status, media_type=media_type,
                    headers={**cors_headers(request), **(headers or {})})


def json_response(request, status, body, extra=None):
    return respond(request, status, json.dumps(body, ensure_ascii=False).encode(),
                   extra, 'application/json')


def yt_error_response(request, status, err, extra=None, error_format=None):
    # X-YT-Error-Format governs the X-YT-Error header only. The real proxy
    # keeps the pre-flush response body as ordinary JSON (context.cpp).
    header_text, header_ctype = format_error_header(err, error_format or ('json', False))
    request.state.audit_extra.setdefault('error_code', err['code'])
    return json_response(request, status, err, {
        'X-YT-Error': header_text,
        'X-YT-Error-Content-Type': header_ctype,
        'X-YT-Response-Code': str(err['code']),
        'X-YT-Response-Message': escape_header_value(err['message']),
        **(extra or {})})


@app.middleware('http')
async def request_pipeline(request, call_next):
    """Body buffering, request log, enveloped 500s, and the audit write.

    The audit entry is emitted before the response is handed to the transport,
    so the trail never lags what a client saw; writes are fail-open. Handlers
    are sync (threadpool) because command logic blocks (PG, PBKDF2, MOCK_DELAY);
    they read the body via request.state.body_buf, buffered here.
    """
    request.state.audit_user = None
    request.state.audit_extra = {}
    if request.method == 'OPTIONS':
        request.state.body_buf = b''
        if request.headers.get('Origin') not in CORS_ORIGINS:
            return Response(b'', status_code=403)
        return await run_dedicated(
            _INFRA_EXECUTOR, _INFRA_CAPACITY, respond, request, 200, b'')
    request.state.body_buf = await request.body()  # uvicorn decodes chunked itself
    log(request.method,
        request.url.path + ('?<omitted>' if request.url.query else ''),
        f'body_bytes={len(request.state.body_buf)}')
    try:
        response = await call_next(request)
    except Exception as e:
        log('  !! internal error', repr(e))
        response = await run_dedicated(
            _INFRA_EXECUTOR, _INFRA_CAPACITY,
            yt_error_response, request, 500, yt_error(1, str(e)))
    path = request.url.path
    if path not in AUDIT_EXEMPT and not path.startswith('/hosts/'):
        try:  # audit is fail-open: a storage outage must not break serving
            await run_audit(
                request.state.audit_user, path,
                {'method': request.method, 'status': response.status_code,
                 **request.state.audit_extra})
        except Exception as e:
            log('  !! audit write failed', repr(e))
    return response


@app.api_route('/ping', methods=METHODS)
async def ping(request: Request):
    return await run_dedicated(
        _INFRA_EXECUTOR, _INFRA_CAPACITY, json_response, request, 200, {})


@app.api_route('/ready', methods=METHODS)
async def ready(request: Request):
    try:
        healthy = await asyncio.wait_for(
            run_dedicated(
                _HEALTH_EXECUTOR, _HEALTH_CAPACITY, userdb.healthy),
            timeout=_HEALTH_TIMEOUT_SECONDS)
    except TimeoutError:
        healthy = False
        log('  !! readiness check timed out')
    except Exception as error:
        healthy = False
        log('  !! readiness check failed', type(error).__name__)
    return await run_dedicated(
        _INFRA_EXECUTOR, _INFRA_CAPACITY,
        json_response, request, 200 if healthy else 503, {})


@app.api_route('/version', methods=METHODS)
@app.api_route('/service/version', methods=METHODS)
async def version(request: Request):
    return await run_dedicated(
        _INFRA_EXECUTOR, _INFRA_CAPACITY,
        respond, request, 200, b'mock-proxy-1.0.0', None, 'text/plain')


@app.api_route('/hosts/all', methods=METHODS)
async def hosts_all(request: Request):
    return await run_dedicated(
        _INFRA_EXECUTOR, _INFRA_CAPACITY, json_response, request, 200, [])


@app.api_route('/hosts', methods=METHODS)
@app.api_route('/hosts/{_rest:path}', methods=METHODS)
async def hosts(request: Request, _rest=''):
    # Role filtering like coordinator.cpp: this mock is one "data"-role proxy
    # (the default role filter); other roles have no members.
    role = request.query_params.get('role', 'data')
    return await run_dedicated(
        _INFRA_EXECUTOR, _INFRA_CAPACITY,
        json_response, request, 200, [HOST] if role == 'data' else [])


@app.api_route('/api', methods=METHODS)
@app.api_route('/api/', methods=METHODS)
async def api_versions(request: Request):
    return await run_dedicated(
        _INFRA_EXECUTOR, _INFRA_CAPACITY,
        json_response, request, 200, ['v3', 'v4'])


@app.api_route('/login', methods=METHODS)
@app.api_route('/login/{_rest:path}', methods=METHODS)
def login(request: Request, _rest=''):
    # HTTP Basic; this deployment adds SameSite=Lax to the real proxy shape.
    authorization = request.headers.get('Authorization')
    if authorization is None:
        return respond(request, 401, b'', {'WWW-Authenticate': 'Basic'})
    if ' ' not in authorization:
        return yt_error_response(request, 400, yt_error(
            1, 'Malformed "Authorization" header: failed to parse authorization method'))
    method, encoded_credentials = authorization.split(' ', 1)
    if method != 'Basic':
        return yt_error_response(
            request, 400, yt_error(1, f'Unsupported authorization method "{method}"'))
    try:
        credentials = base64.b64decode(encoded_credentials, validate=True)
    except (binascii.Error, ValueError):
        return yt_error_response(
            request, 400, yt_error(1, 'Failed to decode user credentials'))
    if b':' not in credentials:
        return yt_error_response(
            request, 400, yt_error(1, 'Failed to parse user credentials'))
    user_bytes, password_bytes = credentials.split(b':', 1)
    user = user_bytes.decode('utf-8', 'replace')
    password = password_bytes.decode('utf-8', 'replace')
    request.state.audit_user = user
    if REQUIRE_AUTH and userdb.is_published_development_credential(user, password):
        request.state.audit_extra['outcome'] = 'rejected'
        return yt_error_response(
            request, 401, yt_error(1, 'Incorrect login or password'),
            {'WWW-Authenticate': 'Basic'})
    cookie = userdb.authenticate_and_create_session(user, password)
    # Locally-added users (test users) never reach the external YTsaurus;
    # everyone else is verified there and provisioned on first success.
    if cookie is None and UPSTREAM and userdb.user_origin(user) != 'local':
        verdict = upstream_login(encoded_credentials)
        if verdict is None:
            request.state.audit_extra['outcome'] = 'upstream_unavailable'
            return yt_error_response(
                request, 503, yt_error(1, 'External authentication is unavailable'))
        if verdict:
            cookie = userdb.external_login(user)
    if cookie is None:
        # Real proxy masks the cause: generic code 1 (cypress_cookie_login.cpp:83).
        request.state.audit_extra['outcome'] = 'rejected'
        return yt_error_response(
            request, 401, yt_error(1, 'Incorrect login or password'),
            {'WWW-Authenticate': 'Basic'})
    request.state.audit_extra.update(outcome='success', origin=userdb.user_origin(user))
    expires = format_datetime(datetime.now(timezone.utc) + userdb.SESSION_TTL, usegmt=True)
    return respond(request, 200, b'', {
        'Set-Cookie': (
            f'YTCypressCookie={cookie}; Expires={expires}; Secure; HttpOnly; '
            'SameSite=Lax; Path=/')})


@app.api_route('/auth/whoami', methods=METHODS)
def whoami(request: Request):  # must succeed with truthy csrf_token even without credentials
    auth = authenticate(request.headers)
    if not auth:
        return yt_error_response(request, 401, yt_error(900, 'Authentication failed'))
    request.state.audit_user = auth['user']
    return json_response(request, 200, {
        'login': auth['user'],
        'realm': 'cypress_cookie' if auth['via_cookie'] else 'mock',
        'real_login': auth['user'], 'csrf_token': csrf_token_for(auth['user'])})


@app.api_route('/api/{version}/{command}', methods=METHODS)
def api_command(request: Request, version: str, command: str):
    if version not in ('v3', 'v4') or not re.fullmatch(r'\w+', command):
        return unhandled(request)
    request.state.audit_extra['command'] = command
    auth = authenticate(request.headers)
    if not auth:
        return yt_error_response(request, 401, yt_error(900, 'Authentication failed'))
    request.state.audit_user = auth['user']
    if csrf_error := check_csrf(request.method, request.headers, auth):
        status, error = csrf_error
        return yt_error_response(request, status, error)

    params = dict(request.query_params)
    if hdr := request.headers.get('X-YT-Parameters'):
        try:
            params.update(json.loads(hdr))
        except ValueError:
            log(f'  !! bad X-YT-Parameters: {hdr[:200]}')
    body_buf = request.state.body_buf
    if body_buf and 'json' in (request.headers.get('Content-Type') or 'json'):
        try:
            body = json.loads(body_buf)
            if isinstance(body, dict):
                params.update(body)
        except ValueError:
            pass  # raw data body
    if 'path' in params:
        request.state.audit_extra['path'] = params['path']
    if command == 'execute_batch' and isinstance(params.get('requests'), list):
        requests = params['requests']
        summaries = [
            {'command': r.get('command'),
             'path': r['parameters'].get('path') if isinstance(r.get('parameters'), dict) else None}
            for r in requests[:8] if isinstance(r, dict)]
        request.state.audit_extra['requests'] = summaries
        if omitted := len(requests) - len(summaries):
            request.state.audit_extra.update(requests_omitted=omitted, _audit_truncated=True)

    impl = COMMANDS.get(command)
    if not impl:
        log(f'  !! unimplemented command: {command}')
        return yt_error_response(
            request, 404, yt_error(1, f'Command {command} is not registered'))
    try:
        error_format = parse_error_format(request.headers)
    except ValueError as error:
        return yt_error_response(request, 400, yt_error(1, str(error)))
    try:
        maybe_delay(command, params)
        result = impl(params, auth)
    except CommandError as e:
        return yt_error_response(request, e.status, e.err, error_format=error_format)
    of = params.get('output_format')
    typed = isinstance(of, dict) and of.get('$attributes', {}).get('annotate_with_types')
    if command in RAW_OUTPUT:
        payload = result
    else:
        payload = typed_annotate(result) if typed else annotated(result)
    if version == 'v4' and command in ('get', 'list', 'exists'):
        payload = {'value': payload}
    return json_response(request, 200, payload, {'X-YT-Proxy': HOST})


@app.api_route('/{_rest:path}', methods=METHODS)
def unhandled(request: Request, _rest=''):
    log('  !! unhandled route')
    if auth := authenticate(request.headers):  # attribute probes when possible
        request.state.audit_user = auth['user']
    return yt_error_response(
        request, 404, yt_error(1, f'No such route: {request.url.path}'))


if __name__ == '__main__':
    log(
        'mock YT proxy (python/fastapi) '
        f'bound to {BIND_HOST}:{PORT}, advertised as http://{HOST}')
    uvicorn.run(
        app, host=BIND_HOST, port=PORT, log_level='warning',
        timeout_keep_alive=5)
