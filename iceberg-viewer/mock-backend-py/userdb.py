#!/usr/bin/env python3
"""User/session store: PostgreSQL when MOCK_PG_DSN is set, in-RAM otherwise.

Users and login sessions are the one piece of real state in the mock; table
data stays fake. CLI: python3 userdb.py add-user <login> [--password-stdin |
--password-file <path>] | list-users
"""
import collections
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone

DSN = os.environ.get('MOCK_PG_DSN')
SESSION_TTL = timedelta(seconds=int(os.environ.get('MOCK_COOKIE_TTL_SECONDS') or 30 * 24 * 3600))
REQUIRE_AUTH = bool(os.environ.get('MOCK_REQUIRE_AUTH'))
if DSN and REQUIRE_AUTH and os.environ.get('PGPASSWORD') == 'mock-password':
    raise RuntimeError(
        'PGPASSWORD must be changed from the published mock-password '
        'placeholder before authenticated database startup')
PUBLISHED_DEV_USERS = {'iceberg': 'iceberg', 'root': ''}
# These well-known accounts exist only for anonymous protocol-fidelity tests.
# Never put their hashes into an authenticated store, even if the opt-in leaks
# into that environment.
ENABLE_DEV_USERS = (
    os.environ.get('MOCK_ENABLE_DEV_SEED_USERS') == '1' and not REQUIRE_AUTH)
SEED_USERS = PUBLISHED_DEV_USERS if ENABLE_DEV_USERS else {}
PASSWORD_SCHEME = 'pbkdf2_sha256'
PBKDF2_ITERATIONS = 600_000
PBKDF2_MAX_VERIFY_ITERATIONS = 5_000_000

# This is a compact-JSON limit for the user-controlled part of one audit row:
# login, endpoint, and details.  Keep the component limits conservative enough
# that the envelope is also strictly smaller than 1,000 bytes.
AUDIT_PAYLOAD_LIMIT_BYTES = 1000
_AUDIT_LOGIN_JSON_BYTES = 120
_AUDIT_ENDPOINT_JSON_BYTES = 240
_AUDIT_DETAILS_JSON_BYTES = 600
_AUDIT_VALUE_JSON_BYTES = 160
_AUDIT_KEY_JSON_BYTES = 64
_AUDIT_MAX_ITEMS = 8
_AUDIT_MAX_DEPTH = 3
_AUDIT_REDACTED = '<redacted>'
_AUDIT_SENSITIVE_KEYS = frozenset({
    'access_token',
    'api_key',
    'authorization',
    'client_secret',
    'cookie',
    'credential',
    'credentials',
    'csrf_token',
    'id_token',
    'passwd',
    'password',
    'private_key',
    'proxy_authorization',
    'refresh_token',
    'robot_token',
    'secret',
    'session',
    'session_id',
    'set_cookie',
    'token',
    'ytcypresscookie',
})
_AUDIT_SENSITIVE_SUFFIXES = (
    '_api_key',
    '_authorization',
    '_cookie',
    '_credential',
    '_credentials',
    '_passwd',
    '_password',
    '_private_key',
    '_secret',
    '_session',
    '_token',
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    login             text PRIMARY KEY,
    salt              text NOT NULL,
    password_hash     text NOT NULL,
    -- 'local': password verified here. 'external': identity verified against a
    -- real YTsaurus (MOCK_YT_UPSTREAM); no password material is stored.
    origin            text NOT NULL DEFAULT 'local',
    password_revision bigint NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'local';
CREATE TABLE IF NOT EXISTS settings (
    key   text PRIMARY KEY,
    value text NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    cookie             text PRIMARY KEY,
    login              text NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    password_revision  bigint NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL);
-- Strict columns only for the essentials; everything with a changing shape
-- goes into the schemaless details. login is NULL for unauthenticated requests.
CREATE TABLE IF NOT EXISTS audit_log (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    login    text,
    endpoint text NOT NULL,
    details  jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts);
"""


def _hash(password, salt, iterations=PBKDF2_ITERATIONS):
    digest = hashlib.pbkdf2_hmac(
        'sha256', password.encode(), salt.encode(), iterations)
    return f'{PASSWORD_SCHEME}${iterations}${digest.hex()}'


def _password_matches(password, salt, password_hash):
    try:
        scheme, raw_iterations, digest = password_hash.split('$')
        iterations = int(raw_iterations)
        if (scheme != PASSWORD_SCHEME
                or not PBKDF2_ITERATIONS <= iterations <= PBKDF2_MAX_VERIFY_ITERATIONS
                or len(bytes.fromhex(digest)) != 32):
            return False
        expected = _hash(password, salt, iterations)
    except (AttributeError, TypeError, ValueError):
        return False
    return secrets.compare_digest(password_hash, expected)


def _new_password(password):
    salt = secrets.token_hex(16)
    return salt, _hash(password, salt)


def is_published_development_credential(login, password):
    """Return whether this is one of the public anonymous-test credentials."""
    # Compare bytes: compare_digest raises on non-ASCII str, and a 500 there
    # would fingerprint which login names are on the published list.
    return (
        isinstance(login, str)
        and isinstance(password, str)
        and login in PUBLISHED_DEV_USERS
        and secrets.compare_digest(
            password.encode(), PUBLISHED_DEV_USERS[login].encode()))


def _is_forbidden_published_credential(login, password):
    """Authenticated mode never accepts the documented development passwords."""
    return REQUIRE_AUTH and is_published_development_credential(login, password)


def _new_cookie(login):
    # GenerateCookieValue parity (cypress_cookie.cpp:47-53): 32 random bytes, hex.
    return secrets.token_hex(32)


def _compact_json(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _json_size(value):
    return len(_compact_json(value).encode('utf-8'))


def _bounded_audit_text(value, encoded_limit):
    """Return text whose JSON representation fits encoded_limit UTF-8 bytes."""
    text = value if isinstance(value, str) else str(value)
    # No valid PostgreSQL jsonb value can contain U+0000.  Normalising invalid
    # surrogates also makes the size calculation and database encoding agree.
    sample = text[:encoded_limit].encode('utf-8', 'replace').decode('utf-8')
    sample = sample.replace('\x00', '\ufffd')
    was_cut = len(text) > encoded_limit
    if not was_cut and _json_size(sample) <= encoded_limit:
        return sample

    suffix = '\u2026'
    low, high = 0, len(sample)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_size(sample[:middle] + suffix) <= encoded_limit:
            low = middle
        else:
            high = middle - 1
    return sample[:low] + suffix


def _is_sensitive_audit_key(key):
    """Match credential-bearing field names without hiding benign metadata."""
    text = key if isinstance(key, str) else str(key)
    # Only the tail is relevant for suffix matching. Bounding it also prevents
    # an attacker-controlled, enormous key from making redaction itself costly.
    text = text[-256:]
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', text)
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', text)
    normalised = re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')
    return (
        normalised in _AUDIT_SENSITIVE_KEYS
        or normalised.endswith(_AUDIT_SENSITIVE_SUFFIXES))


def _normalise_audit_value(value, depth=0):
    """Copy only a bounded JSON prefix and report whether anything was omitted."""
    if isinstance(value, str):
        safe = _bounded_audit_text(value, _AUDIT_VALUE_JSON_BYTES)
        return safe, safe != value
    if value is None or isinstance(value, (bool, int)):
        if isinstance(value, int) and value.bit_length() > 256:
            return '<large integer>', True
        return value, False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (str(value), True)
    if depth >= _AUDIT_MAX_DEPTH:
        if isinstance(value, (dict, list, tuple)):
            return {'item_count': len(value)}, True
        return _bounded_audit_text(value, _AUDIT_VALUE_JSON_BYTES), True
    if isinstance(value, dict):
        result = {}
        truncated = len(value) > _AUDIT_MAX_ITEMS
        for index, (key, child) in enumerate(value.items()):
            if index >= _AUDIT_MAX_ITEMS:
                break
            safe_key = _bounded_audit_text(key, _AUDIT_KEY_JSON_BYTES)
            if safe_key in result:
                truncated = True
                continue
            if _is_sensitive_audit_key(key):
                result[safe_key] = _AUDIT_REDACTED
                truncated = True
                continue
            if depth == 0 and safe_key == 'requests' and isinstance(child, (list, tuple)):
                summaries, omitted, child_truncated = _normalise_batch_requests(child)
                result[safe_key] = summaries
                if omitted:
                    result['requests_omitted'] = omitted
                truncated = truncated or child_truncated
                continue
            safe_child, child_truncated = _normalise_audit_value(child, depth + 1)
            result[safe_key] = safe_child
            truncated = truncated or child_truncated or safe_key != key
        return result, truncated
    if isinstance(value, (list, tuple)):
        result = []
        truncated = len(value) > _AUDIT_MAX_ITEMS
        for child in value[:_AUDIT_MAX_ITEMS]:
            safe_child, child_truncated = _normalise_audit_value(child, depth + 1)
            result.append(safe_child)
            truncated = truncated or child_truncated
        return result, truncated
    return _bounded_audit_text(value, _AUDIT_VALUE_JSON_BYTES), True


def _normalise_batch_requests(requests):
    allowed = ('command', 'path', 'status', 'error_code')
    summaries = []
    truncated = len(requests) > _AUDIT_MAX_ITEMS
    for request in requests[:_AUDIT_MAX_ITEMS]:
        if not isinstance(request, dict):
            truncated = True
            continue
        summary = {}
        truncated = truncated or len(request) > sum(key in request for key in allowed)
        for key in allowed:
            if key in request:
                summary[key], child_truncated = _normalise_audit_value(request[key], 2)
                truncated = truncated or child_truncated
        summaries.append(summary)
    omitted = len(requests) - len(summaries)
    return summaries, omitted, truncated


def _audit_scalar(value):
    if isinstance(value, str):
        return _bounded_audit_text(value, 80)
    if value is None or isinstance(value, (bool, int)):
        return (value if not isinstance(value, int) or value.bit_length() <= 256
                else '<large integer>')
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _bounded_audit_text(value, 80)


def _bounded_audit_details(details):
    details, truncated = _normalise_audit_value(details)
    if truncated:
        details = ({**details, '_audit_truncated': True}
                   if isinstance(details, dict)
                   else {'value': details, '_audit_truncated': True})
    if _json_size(details) <= _AUDIT_DETAILS_JSON_BYTES:
        return details

    # Oversized data becomes an allowlisted summary. In particular, a future
    # caller cannot accidentally retain complete execute_batch requests.
    result = {'_audit_truncated': True}
    if not isinstance(details, dict):
        return result
    for key in ('method', 'status', 'outcome', 'origin',
                'command', 'path', 'error_code'):
        if key in details:
            candidate = {**result, key: _audit_scalar(details[key])}
            if _json_size(candidate) <= _AUDIT_DETAILS_JSON_BYTES:
                result = candidate

    requests = details.get('requests')
    if not isinstance(requests, list):
        return result
    previously_omitted = details.get('requests_omitted', 0)
    if not isinstance(previously_omitted, int) or previously_omitted < 0:
        previously_omitted = 0
    total = len(requests) + previously_omitted
    batch = {**result, 'requests': [], 'requests_omitted': total}
    if _json_size(batch) > _AUDIT_DETAILS_JSON_BYTES:
        return result
    result = batch
    for request in requests[:_AUDIT_MAX_ITEMS]:
        if not isinstance(request, dict):
            continue
        summary = {key: _audit_scalar(request[key])
                   for key in ('command', 'path', 'status', 'error_code')
                   if key in request}
        kept = [*result['requests'], summary]
        candidate = {**result, 'requests': kept,
                     'requests_omitted': total - len(kept)}
        if not candidate['requests_omitted']:
            del candidate['requests_omitted']
        if _json_size(candidate) > _AUDIT_DETAILS_JSON_BYTES:
            break
        result = candidate
    return result


def _sanitize_audit(login, endpoint, details):
    safe_login = (
        None if login is None
        else _bounded_audit_text(login, _AUDIT_LOGIN_JSON_BYTES))
    safe_endpoint = _bounded_audit_text(endpoint, _AUDIT_ENDPOINT_JSON_BYTES)
    safe_details = _bounded_audit_details(details)
    payload = {'login': safe_login, 'endpoint': safe_endpoint, 'details': safe_details}
    # The component limits currently leave seven bytes of headroom. Keep a
    # runtime fallback too, so optimized Python and future field changes cannot
    # silently disable the storage bound.
    if _json_size(payload) >= AUDIT_PAYLOAD_LIMIT_BYTES:
        safe_details = {'_audit_truncated': True}
    return safe_login, safe_endpoint, safe_details


if DSN:
    import psycopg

    _conn = None
    _lock = threading.RLock()  # serialize use and replacement of the connection
    _RECONNECT_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)

    def _connect():
        conn = psycopg.connect(DSN, autocommit=True)
        try:
            conn.execute(SCHEMA)
            for login, password in SEED_USERS.items():
                salt, password_hash = _new_password(password)
                conn.execute(
                    'INSERT INTO users (login, salt, password_hash) VALUES (%s, %s, %s)'
                    ' ON CONFLICT (login) DO NOTHING',
                    (login, salt, password_hash))
        except Exception:
            conn.close()
            raise
        return conn

    def _discard_connection():
        global _conn
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None

    def _query(sql, params=(), retry=True):
        global _conn
        with _lock:
            attempts = 2 if retry else 1
            for attempt in range(attempts):
                try:
                    if _conn is None or _conn.closed:
                        _conn = _connect()
                    return _conn.execute(sql, params).fetchall()
                except _RECONNECT_ERRORS:
                    _discard_connection()
                    if attempt + 1 == attempts:
                        raise

    def healthy():
        try:
            return bool(_query('SELECT 1'))
        except psycopg.Error:
            return False

    def user_exists(login):
        return bool(_query('SELECT 1 FROM users WHERE login = %s', (login,)))

    def user_origin(login):
        rows = _query('SELECT origin FROM users WHERE login = %s', (login,))
        return rows[0][0] if rows else None

    def external_login(login):
        """Provision (once) an externally-verified user and open a session."""
        _query("INSERT INTO users (login, salt, password_hash, origin)"
               " VALUES (%s, '', '', 'external')"
               ' ON CONFLICT (login) DO NOTHING RETURNING login',
               (login,), retry=False)
        return create_session(login) if user_origin(login) == 'external' else None

    def verify(login, password):
        if _is_forbidden_published_credential(login, password):
            return False
        rows = _query('SELECT salt, password_hash FROM users WHERE login = %s', (login,))
        if not rows:
            return False

        salt, password_hash = rows[0]
        return _password_matches(password, salt, password_hash)

    def create_session(login):
        cookie = _new_cookie(login)
        rows = _query(
            'WITH expired AS (DELETE FROM sessions WHERE expires_at <= now())'
            ' INSERT INTO sessions (cookie, login, password_revision, expires_at)'
            ' SELECT %s, login, password_revision, %s FROM users WHERE login = %s'
            ' RETURNING cookie',
            (cookie, datetime.now(timezone.utc) + SESSION_TTL, login),
            retry=False)
        return rows[0][0] if rows else None

    def authenticate_and_create_session(login, password):
        if _is_forbidden_published_credential(login, password):
            return None
        rows = _query(
            'SELECT salt, password_hash, password_revision'
            ' FROM users WHERE login = %s',
            (login,))
        if not rows:
            return None

        salt, password_hash, password_revision = rows[0]
        if not _password_matches(password, salt, password_hash):
            return None

        cookie = _new_cookie(login)
        created = _query(
            'WITH expired AS (DELETE FROM sessions WHERE expires_at <= now())'
            ' INSERT INTO sessions (cookie, login, password_revision, expires_at)'
            ' SELECT %s, login, password_revision, %s FROM users'
            ' WHERE login = %s AND salt = %s AND password_hash = %s'
            ' AND password_revision = %s'
            ' RETURNING cookie',
            (cookie, datetime.now(timezone.utc) + SESSION_TTL,
             login, salt, password_hash, password_revision),
            retry=False)
        return created[0][0] if created else None

    def session_user(cookie):
        rows = _query(
            'SELECT sessions.login FROM sessions'
            ' JOIN users ON users.login = sessions.login'
            ' WHERE sessions.cookie = %s AND sessions.expires_at > now()'
            ' AND sessions.password_revision = users.password_revision',
            (cookie,))
        return rows[0][0] if rows else None

    def session_info(cookie):
        rows = _query(
            'SELECT sessions.login, sessions.created_at, sessions.expires_at'
            ' FROM sessions JOIN users ON users.login = sessions.login'
            ' WHERE sessions.cookie = %s AND sessions.expires_at > now()'
            ' AND sessions.password_revision = users.password_revision',
            (cookie,))
        return rows[0] if rows else None

    def csrf_secret():
        if secret := os.environ.get('MOCK_CSRF_SECRET'):
            return secret
        _query("INSERT INTO settings (key, value) VALUES ('csrf_secret', %s)"
               ' ON CONFLICT (key) DO NOTHING RETURNING key',
               (secrets.token_hex(32),), retry=False)
        return _query("SELECT value FROM settings WHERE key = 'csrf_secret'")[0][0]

    def set_password(login, password):
        salt, password_hash = _new_password(password)
        _query('WITH changed AS ('
               " INSERT INTO users (login, salt, password_hash) VALUES (%s, %s, %s)"
               ' ON CONFLICT (login) DO UPDATE'
               " SET salt = EXCLUDED.salt, password_hash = EXCLUDED.password_hash,"
               " origin = 'local',"
               ' password_revision = users.password_revision + 1'
               ' RETURNING login)'
               ' DELETE FROM sessions WHERE login = (SELECT login FROM changed)'
               ' RETURNING cookie',
               (login, salt, password_hash),
               retry=False)

    def list_users():
        return [login if origin == 'local' else f'{login} ({origin})'
                for login, origin in _query('SELECT login, origin FROM users ORDER BY login')]

    def audit(login, endpoint, details):
        login, endpoint, details = _sanitize_audit(login, endpoint, details)
        _query('INSERT INTO audit_log (login, endpoint, details)'
               ' VALUES (%s, %s, %s::jsonb) RETURNING id',
               (login, endpoint, _compact_json(details)), retry=False)

    def audit_tail(count):
        rows = _query('SELECT ts, login, endpoint, details FROM audit_log'
                      ' ORDER BY id DESC LIMIT %s', (count,))
        return rows[::-1]

    def audit_rows():
        """Non-sensitive columns only — feeds the //sys/logs/audit_log table."""
        return _query('SELECT ts, login, endpoint FROM audit_log ORDER BY id')

else:  # in-RAM fallback: same behavior, nothing persisted
    _users = {}
    _origins = {}
    _password_revisions = {}
    for _login, _password in SEED_USERS.items():
        _users[_login] = _new_password(_password)
        _password_revisions[_login] = 0
    _sessions = {}
    _lock = threading.RLock()

    def healthy():
        return True

    def user_exists(login):
        with _lock:
            return login in _users

    def user_origin(login):
        with _lock:
            return _origins.get(login, 'local') if login in _users else None

    def external_login(login):
        """Provision (once) an externally-verified user and open a session."""
        with _lock:
            if login in _users and _origins.get(login, 'local') == 'local':
                return None
            if login not in _users:
                _users[login] = ('', '')  # unmatchable: never verifies locally
                _origins[login] = 'external'
                _password_revisions[login] = 0
            return _create_session_locked(login)

    def verify(login, password):
        if _is_forbidden_published_credential(login, password):
            return False
        with _lock:
            if login not in _users:
                return False
            salt, password_hash = _users[login]
            return _password_matches(password, salt, password_hash)

    _csrf_secret = os.environ.get('MOCK_CSRF_SECRET') or secrets.token_hex(32)

    def _create_session_locked(login):
        cookie = _new_cookie(login)
        now = datetime.now(timezone.utc)
        for expired_cookie, info in list(_sessions.items()):
            if info[3] <= now:
                del _sessions[expired_cookie]
        _sessions[cookie] = (
            login, _password_revisions.get(login, 0), now, now + SESSION_TTL)
        return cookie

    def create_session(login):
        with _lock:
            if login not in _users:
                return None
            return _create_session_locked(login)

    def authenticate_and_create_session(login, password):
        if _is_forbidden_published_credential(login, password):
            return None
        with _lock:
            credentials = _users.get(login)
            if not credentials:
                return None
            salt, password_hash = credentials
            if not _password_matches(password, salt, password_hash):
                return None
            return _create_session_locked(login)

    def session_user(cookie):
        info = session_info(cookie)
        return info[0] if info else None

    def session_info(cookie):
        with _lock:
            info = _sessions.get(cookie)
            if info and info[3] <= datetime.now(timezone.utc):
                del _sessions[cookie]
                return None
            if info and (
                    info[0] not in _users
                    or info[1] != _password_revisions.get(info[0], 0)):
                del _sessions[cookie]
                return None
            return (info[0], info[2], info[3]) if info else None

    def csrf_secret():
        return _csrf_secret

    def set_password(login, password):
        with _lock:
            if login in _users:
                _password_revisions[login] = _password_revisions.get(login, 0) + 1
            else:
                _password_revisions[login] = 0
            _users[login] = _new_password(password)
            _origins[login] = 'local'
            for cookie, info in list(_sessions.items()):
                if info[0] == login:
                    del _sessions[cookie]

    def list_users():
        with _lock:
            return sorted(
                login if _origins.get(login, 'local') == 'local' else f'{login} (external)'
                for login in _users)

    _audit = collections.deque(maxlen=10_000)

    def audit(login, endpoint, details):
        login, endpoint, details = _sanitize_audit(login, endpoint, details)
        with _lock:
            _audit.append((datetime.now(timezone.utc), login, endpoint, details))

    def audit_tail(count):
        with _lock:
            return list(_audit)[-count:]

    def audit_rows():
        """Non-sensitive columns only — feeds the //sys/logs/audit_log table."""
        with _lock:
            return [(ts, login, endpoint) for ts, login, endpoint, _ in _audit]


if __name__ == '__main__':
    def password_from_cli(arguments):
        if not arguments:
            if not sys.stdin.isatty():
                sys.exit(
                    'non-interactive add-user requires --password-stdin or '
                    '--password-file')
            import getpass
            return getpass.getpass('Password: ')
        if arguments == ['--password-stdin']:
            password = sys.stdin.read()
        elif len(arguments) == 2 and arguments[0] == '--password-file':
            with open(arguments[1], encoding='utf-8') as password_file:
                password = password_file.read()
        else:
            sys.exit(
                'usage: userdb.py add-user <login> [--password-stdin | '
                '--password-file <path>]')
        # Secret files and pipes conventionally have one line terminator. Do
        # not otherwise strip whitespace, which may intentionally be part of
        # the password.
        if password.endswith('\n'):
            password = password[:-1]
            if password.endswith('\r'):
                password = password[:-1]
        return password

    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'add-user' and len(sys.argv) >= 3:
        set_password(sys.argv[2], password_from_cli(sys.argv[3:]))
        print(f'user {sys.argv[2]} saved ({"postgres" if DSN else "RAM only — set MOCK_PG_DSN"})')
    elif cmd == 'list-users':
        print('\n'.join(list_users()))
    elif cmd == 'audit-tail':
        for ts, login, endpoint, details in audit_tail(int(sys.argv[2]) if len(sys.argv) > 2 else 20):
            print(json.dumps({'ts': ts.isoformat(), 'user': login, 'endpoint': endpoint,
                              'details': details}, ensure_ascii=False))
    else:
        sys.exit(
            'usage: userdb.py add-user <login> [--password-stdin | '
            '--password-file <path>] | list-users | audit-tail [n]')
