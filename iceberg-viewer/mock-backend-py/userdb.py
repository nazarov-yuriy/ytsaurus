#!/usr/bin/env python3
"""User/session store: PostgreSQL when MOCK_PG_DSN is set, in-RAM otherwise.

Users and login sessions are the one piece of real state in the mock; table
data stays fake. CLI: python3 userdb.py add-user <login> <password> | list-users
"""
import hashlib
import os
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone

DSN = os.environ.get('MOCK_PG_DSN')
SESSION_TTL = timedelta(seconds=int(os.environ.get('MOCK_COOKIE_TTL_SECONDS') or 30 * 24 * 3600))
SEED_USERS = {'iceberg': 'iceberg', 'root': ''}
PASSWORD_SCHEME = 'pbkdf2_sha256'
PBKDF2_ITERATIONS = 600_000
PBKDF2_MAX_VERIFY_ITERATIONS = 5_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    login         text PRIMARY KEY,
    salt          text NOT NULL,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS settings (
    key   text PRIMARY KEY,
    value text NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    cookie     text PRIMARY KEY,
    login      text NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL);
"""


def _legacy_hash(password, salt):
    return hashlib.sha256(f'{salt}:{password}'.encode()).hexdigest()


def _hash(password, salt, iterations=PBKDF2_ITERATIONS):
    digest = hashlib.pbkdf2_hmac(
        'sha256', password.encode(), salt.encode(), iterations)
    return f'{PASSWORD_SCHEME}${iterations}${digest.hex()}'


def _password_matches(password, salt, password_hash):
    if isinstance(password_hash, str) and password_hash.startswith(f'{PASSWORD_SCHEME}$'):
        try:
            scheme, raw_iterations, digest = password_hash.split('$')
            iterations = int(raw_iterations)
            if (scheme != PASSWORD_SCHEME
                    or not 0 < iterations <= PBKDF2_MAX_VERIFY_ITERATIONS
                    or len(bytes.fromhex(digest)) != 32):
                return False
            expected = _hash(password, salt, iterations)
        except (TypeError, ValueError):
            return False
        return secrets.compare_digest(password_hash, expected)

    # Compatibility with hashes created before the PBKDF2 migration.
    if not isinstance(password_hash, str) or len(password_hash) != 64:
        return False
    return secrets.compare_digest(password_hash, _legacy_hash(password, salt))


def _password_needs_upgrade(password_hash):
    try:
        scheme, raw_iterations, _ = password_hash.split('$')
        return scheme != PASSWORD_SCHEME or int(raw_iterations) < PBKDF2_ITERATIONS
    except (AttributeError, TypeError, ValueError):
        return True


def _new_password(password):
    salt = secrets.token_hex(16)
    return salt, _hash(password, salt)


def _new_cookie(login):
    # GenerateCookieValue parity (cypress_cookie.cpp:47-53): 32 random bytes, hex.
    return secrets.token_hex(32)


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

    def verify(login, password):
        rows = _query('SELECT salt, password_hash FROM users WHERE login = %s', (login,))
        if not rows:
            return False

        salt, password_hash = rows[0]
        if not _password_matches(password, salt, password_hash):
            return False
        if _password_needs_upgrade(password_hash):
            new_salt, new_hash = _new_password(password)
            upgraded = _query(
                'UPDATE users SET salt = %s, password_hash = %s'
                ' WHERE login = %s AND salt = %s AND password_hash = %s'
                ' RETURNING login',
                (new_salt, new_hash, login, salt, password_hash),
                retry=False)
            if not upgraded:
                return False
        return True

    def create_session(login):
        cookie = _new_cookie(login)
        _query('INSERT INTO sessions (cookie, login, expires_at) VALUES (%s, %s, %s) RETURNING cookie',
               (cookie, login, datetime.now(timezone.utc) + SESSION_TTL),
               retry=False)
        return cookie

    def session_user(cookie):
        rows = _query('SELECT login FROM sessions WHERE cookie = %s AND expires_at > now()', (cookie,))
        return rows[0][0] if rows else None

    def session_info(cookie):
        rows = _query('SELECT login, created_at, expires_at FROM sessions'
                      ' WHERE cookie = %s AND expires_at > now()', (cookie,))
        return rows[0] if rows else None

    def list_sessions():
        return _query('SELECT cookie, login, created_at, expires_at FROM sessions'
                      ' WHERE expires_at > now() ORDER BY cookie')

    def csrf_secret():
        if secret := os.environ.get('MOCK_CSRF_SECRET'):
            return secret
        _query("INSERT INTO settings (key, value) VALUES ('csrf_secret', %s)"
               ' ON CONFLICT (key) DO NOTHING RETURNING key',
               (secrets.token_hex(32),), retry=False)
        return _query("SELECT value FROM settings WHERE key = 'csrf_secret'")[0][0]

    def set_password(login, password):
        salt, password_hash = _new_password(password)
        _query('INSERT INTO users (login, salt, password_hash) VALUES (%s, %s, %s)'
               ' ON CONFLICT (login) DO UPDATE'
               ' SET salt = EXCLUDED.salt, password_hash = EXCLUDED.password_hash'
               ' RETURNING login',
               (login, salt, password_hash),
               retry=False)

    def list_users():
        return [r[0] for r in _query('SELECT login FROM users ORDER BY login')]

else:  # in-RAM fallback: same behavior, nothing persisted
    _users = {}
    for _login, _password in SEED_USERS.items():
        _users[_login] = _new_password(_password)
    _sessions = {}
    _lock = threading.RLock()

    def healthy():
        return True

    def user_exists(login):
        with _lock:
            return login in _users

    def verify(login, password):
        with _lock:
            if login not in _users:
                return False
            salt, password_hash = _users[login]
            if not _password_matches(password, salt, password_hash):
                return False
            if _password_needs_upgrade(password_hash):
                _users[login] = _new_password(password)
            return True

    _csrf_secret = os.environ.get('MOCK_CSRF_SECRET') or secrets.token_hex(32)

    def create_session(login):
        cookie = _new_cookie(login)
        now = datetime.now(timezone.utc)
        with _lock:
            _sessions[cookie] = (login, now, now + SESSION_TTL)
        return cookie

    def session_user(cookie):
        info = session_info(cookie)
        return info[0] if info else None

    def session_info(cookie):
        with _lock:
            info = _sessions.get(cookie)
        if info and info[2] > datetime.now(timezone.utc):
            return info
        return None

    def list_sessions():
        now = datetime.now(timezone.utc)
        with _lock:
            return sorted((c, *info) for c, info in _sessions.items() if info[2] > now)

    def csrf_secret():
        return _csrf_secret

    def set_password(login, password):
        with _lock:
            _users[login] = _new_password(password)

    def list_users():
        with _lock:
            return sorted(_users)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'add-user' and len(sys.argv) == 4:
        set_password(sys.argv[2], sys.argv[3])
        print(f'user {sys.argv[2]} saved ({"postgres" if DSN else "RAM only — set MOCK_PG_DSN"})')
    elif cmd == 'list-users':
        print('\n'.join(list_users()))
    else:
        sys.exit('usage: userdb.py add-user <login> <password> | list-users')
