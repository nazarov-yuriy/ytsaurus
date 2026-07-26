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
    login             text PRIMARY KEY,
    salt              text NOT NULL,
    password_hash     text NOT NULL,
    password_revision bigint NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS settings (
    key   text PRIMARY KEY,
    value text NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    cookie             text PRIMARY KEY,
    login              text NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    password_revision  bigint NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL);
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
               ' INSERT INTO users (login, salt, password_hash) VALUES (%s, %s, %s)'
               ' ON CONFLICT (login) DO UPDATE'
               ' SET salt = EXCLUDED.salt, password_hash = EXCLUDED.password_hash,'
               ' password_revision = users.password_revision + 1'
               ' RETURNING login)'
               ' DELETE FROM sessions WHERE login = (SELECT login FROM changed)'
               ' RETURNING cookie',
               (login, salt, password_hash),
               retry=False)

    def list_users():
        return [r[0] for r in _query('SELECT login FROM users ORDER BY login')]

else:  # in-RAM fallback: same behavior, nothing persisted
    _users = {}
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

    def verify(login, password):
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
            for cookie, info in list(_sessions.items()):
                if info[0] == login:
                    del _sessions[cookie]

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
