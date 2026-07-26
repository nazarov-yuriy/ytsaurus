#!/usr/bin/env python3
"""User-persistence tests for the Python mock backend with PostgreSQL.

Requires a reachable PostgreSQL and psycopg:
    MOCK_PG_TEST_DSN=postgresql://... python3 tests/test_user_persistence.py
Skips cleanly when the DSN or psycopg is absent. The interpreter must have
psycopg installed (the server subprocesses inherit it via sys.executable).

Covers what in-RAM mode cannot: sessions surviving a full server restart,
users added out-of-band (userdb.py CLI) being visible immediately, and
passwords being stored salted+hashed rather than in plaintext.
"""
import base64
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'mock-backend-py'
DSN = os.environ.get('MOCK_PG_TEST_DSN')
PORT = 8021

try:
    import psycopg
except ImportError:
    psycopg = None


def call(method, path, headers=None):
    req = urllib.request.Request(f'http://localhost:{PORT}{path}', headers=headers or {}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read() or b'null'), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'null'), e.headers


def start_server():
    proc = subprocess.Popen([sys.executable, str(BACKEND / 'server.py'), str(PORT)],
                            env={**os.environ, 'MOCK_PG_DSN': DSN},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f'http://localhost:{PORT}/ping', timeout=1)
            return proc
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError('server did not start')


def basic(user, password):
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()}


@unittest.skipUnless(DSN and psycopg, 'MOCK_PG_TEST_DSN or psycopg not available')
class TestUserPersistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute('DROP TABLE IF EXISTS sessions; DROP TABLE IF EXISTS users CASCADE')
        cls.proc = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()

    def login(self, user, password):
        status, _, hdrs = call('POST', '/login', basic(user, password))
        cookie = (hdrs.get('Set-Cookie') or '').split(';')[0]
        return status, cookie

    def test_1_seed_user_can_login(self):
        status, cookie = self.login('iceberg', 'iceberg')
        self.assertEqual(status, 200)
        _, who, _ = call('GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(who['realm'], 'cypress_cookie')
        self.assertEqual(who['login'], 'iceberg')

    def test_2_wrong_password_is_masked_401(self):
        status, _ = self.login('iceberg', 'nope')
        self.assertEqual(status, 401)

    def test_3_session_survives_server_restart(self):
        _, cookie = self.login('iceberg', 'iceberg')
        type(self).proc.terminate()
        type(self).proc.wait()
        type(self).proc = start_server()  # fresh process, empty RAM
        _, who, _ = call('GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(who['realm'], 'cypress_cookie')  # session came from PG

    def test_4_cli_added_user_is_visible_without_restart(self):
        subprocess.run([sys.executable, str(BACKEND / 'userdb.py'), 'add-user', 'alice', 's3cret'],
                       env={**os.environ, 'MOCK_PG_DSN': DSN}, check=True,
                       stdout=subprocess.DEVNULL)
        status, cookie = self.login('alice', 's3cret')
        self.assertEqual(status, 200)
        _, who, _ = call('GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(who['login'], 'alice')

    def test_5_passwords_are_hashed_at_rest(self):
        with psycopg.connect(DSN) as conn:
            rows = conn.execute('SELECT login, salt, password_hash FROM users').fetchall()
        self.assertGreaterEqual(len(rows), 2)
        for login, salt, password_hash in rows:
            self.assertTrue(salt)
            self.assertEqual(len(password_hash), 64)  # sha256 hex
            self.assertNotIn(password_hash, ('iceberg', 's3cret', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
