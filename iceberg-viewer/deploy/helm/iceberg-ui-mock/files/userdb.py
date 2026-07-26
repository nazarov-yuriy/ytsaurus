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
SESSION_TTL = timedelta(days=30)
SEED_USERS = {'iceberg': 'iceberg', 'root': ''}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    login         text PRIMARY KEY,
    salt          text NOT NULL,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS sessions (
    cookie     text PRIMARY KEY,
    login      text NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL);
"""


def _hash(password, salt):
    return hashlib.sha256(f'{salt}:{password}'.encode()).hexdigest()


def _new_cookie(login):
    return f'mock-{login}-{secrets.token_hex(16)}'


if DSN:
    import psycopg

    _conn = psycopg.connect(DSN, autocommit=True)
    _lock = threading.Lock()  # one shared connection; serialize queries
    with _lock:
        _conn.execute(SCHEMA)
        for login, password in SEED_USERS.items():
            salt = secrets.token_hex(8)
            _conn.execute(
                'INSERT INTO users (login, salt, password_hash) VALUES (%s, %s, %s)'
                ' ON CONFLICT (login) DO NOTHING', (login, salt, _hash(password, salt)))

    def _query(sql, params=()):
        with _lock:
            return _conn.execute(sql, params).fetchall()

    def user_exists(login):
        return bool(_query('SELECT 1 FROM users WHERE login = %s', (login,)))

    def verify(login, password):
        rows = _query('SELECT salt, password_hash FROM users WHERE login = %s', (login,))
        return bool(rows) and secrets.compare_digest(rows[0][1], _hash(password, rows[0][0]))

    def create_session(login):
        cookie = _new_cookie(login)
        _query('INSERT INTO sessions (cookie, login, expires_at) VALUES (%s, %s, %s) RETURNING cookie',
               (cookie, login, datetime.now(timezone.utc) + SESSION_TTL))
        return cookie

    def session_user(cookie):
        rows = _query('SELECT login FROM sessions WHERE cookie = %s AND expires_at > now()', (cookie,))
        return rows[0][0] if rows else None

    def set_password(login, password):
        salt = secrets.token_hex(8)
        _query('INSERT INTO users (login, salt, password_hash) VALUES (%s, %s, %s)'
               ' ON CONFLICT (login) DO UPDATE SET salt = %s, password_hash = %s RETURNING login',
               (login, salt, _hash(password, salt), salt, _hash(password, salt)))

    def list_users():
        return [r[0] for r in _query('SELECT login FROM users ORDER BY login')]

else:  # in-RAM fallback: same behavior, nothing persisted
    _users = {login: (secrets.token_hex(8), None) for login in SEED_USERS}
    _users = {login: (salt, _hash(SEED_USERS[login], salt)) for login, (salt, _) in _users.items()}
    _sessions = {}

    def user_exists(login):
        return login in _users

    def verify(login, password):
        return login in _users and _users[login][1] == _hash(password, _users[login][0])

    def create_session(login):
        cookie = _new_cookie(login)
        _sessions[cookie] = login
        return cookie

    def session_user(cookie):
        return _sessions.get(cookie)

    def set_password(login, password):
        salt = secrets.token_hex(8)
        _users[login] = (salt, _hash(password, salt))

    def list_users():
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
