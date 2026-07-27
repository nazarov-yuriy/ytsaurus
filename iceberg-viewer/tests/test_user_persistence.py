#!/usr/bin/env python3
"""User-persistence tests for the Python mock backend with PostgreSQL.

Requires a reachable PostgreSQL and psycopg:
    MOCK_PG_TEST_DSN=postgresql://... python3 tests/test_user_persistence.py
Skips cleanly when the DSN or psycopg is absent. The interpreter must have
psycopg installed (the server subprocesses inherit it via sys.executable).

Covers what in-RAM mode cannot: sessions surviving a full server restart,
users added out-of-band (userdb.py CLI) being visible immediately, and
passwords being stored with salted PBKDF2 rather than in plaintext.
"""
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'mock-backend-py'
DSN = os.environ.get('MOCK_PG_TEST_DSN')
PORT = None
LOCAL_USER = 'local-test'
LOCAL_PASSWORD = 'local-secret'

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
except ImportError:
    psycopg = None
    sql = None
    make_conninfo = None


def call(method, path, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request_headers = dict(headers or {})
    if body is not None:
        request_headers.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(
        f'http://localhost:{PORT}{path}', data=data,
        headers=request_headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read() or b'null'), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'null'), e.headers


def storage_env(dsn):
    env = {
        **{
            key: value for key, value in os.environ.items()
            if key not in (
                'MOCK_ENABLE_DEV_SEED_USERS',
                'MOCK_PG_DSN',
                'MOCK_REQUIRE_AUTH',
                'MOCK_ROBOT_TOKEN',
            )
        },
        'MOCK_PG_DSN': dsn,
        'MOCK_REQUIRE_AUTH': '1',
    }
    return env


def add_user(dsn, login, password):
    subprocess.run(
        [
            sys.executable,
            str(BACKEND / 'userdb.py'),
            'add-user',
            login,
            '--password-stdin',
        ],
        env=storage_env(dsn), input=password + '\n', text=True, check=True,
        stdout=subprocess.DEVNULL)


def start_server(dsn):
    env = {
        **storage_env(dsn),
        'MOCK_ROBOT_TOKEN': 'persistence-test-robot',
    }
    proc = subprocess.Popen([sys.executable, str(BACKEND / 'server.py'), str(PORT)],
                            env=env,
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
        global PORT
        with socket.socket() as sock:
            sock.bind(('localhost', 0))
            PORT = sock.getsockname()[1]
        cls.schema = f'iceberg_mock_test_{os.getpid()}_{secrets.token_hex(4)}'
        cls.application_name = f'iceberg-mock-test-{secrets.token_hex(4)}'
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        try:
            cls.dsn = make_conninfo(
                DSN,
                options=f'-c search_path={cls.schema}',
                application_name=cls.application_name)
            cls.proc = start_server(cls.dsn)
            add_user(cls.dsn, LOCAL_USER, LOCAL_PASSWORD)
        except Exception:
            cls.drop_schema()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.drop_schema()

    @classmethod
    def drop_schema(cls):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
                    sql.Identifier(cls.schema)))

    def login(self, user, password):
        status, _, hdrs = call('POST', '/login', basic(user, password))
        cookie = (hdrs.get('Set-Cookie') or '').split(';')[0]
        return status, cookie

    def test_0_strict_auth_requires_cookie_or_robot_token(self):
        status, body, _ = call('GET', '/auth/whoami')
        self.assertEqual(status, 401)
        self.assertEqual(body['code'], 900)
        status, body, _ = call(
            'POST', '/api/v3/exists', body={'path': '//tmp'})
        self.assertEqual(status, 401)
        self.assertEqual(body['code'], 900)

        robot = {'Authorization': 'OAuth persistence-test-robot'}
        status, who, _ = call('GET', '/auth/whoami', robot)
        self.assertEqual(status, 200)
        self.assertEqual(who['login'], 'iceberg')
        status, _, _ = call(
            'GET', '/auth/whoami',
            {'Authorization': 'OAuth wrong-robot-token'})
        self.assertEqual(status, 401)

    def test_1_published_development_credentials_are_absent(self):
        self.assertEqual(self.login('iceberg', 'iceberg')[0], 401)
        self.assertEqual(self.login('root', '')[0], 401)

        with psycopg.connect(type(self).dsn) as conn:
            rows = conn.execute(
                "SELECT login FROM users WHERE login IN ('iceberg', 'root')"
            ).fetchall()
        self.assertEqual(rows, [])

    def test_2_explicitly_provisioned_local_user_can_login(self):
        status, cookie = self.login(LOCAL_USER, LOCAL_PASSWORD)
        self.assertEqual(status, 200)
        _, who, _ = call('GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(who['realm'], 'cypress_cookie')
        self.assertEqual(who['login'], LOCAL_USER)

    def test_2b_wrong_password_is_masked_401(self):
        status, _ = self.login(LOCAL_USER, 'nope')
        self.assertEqual(status, 401)

    def test_3_session_survives_server_restart(self):
        _, cookie = self.login(LOCAL_USER, LOCAL_PASSWORD)
        type(self).proc.terminate()
        type(self).proc.wait()
        type(self).proc = start_server(type(self).dsn)  # fresh process, empty RAM
        _, who, _ = call('GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(who['realm'], 'cypress_cookie')  # session came from PG

    def test_4_cli_added_user_is_visible_without_restart(self):
        add_user(type(self).dsn, 'alice', 's3cret')
        status, cookie = self.login('alice', 's3cret')
        self.assertEqual(status, 200)
        _, who, _ = call('GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(who['login'], 'alice')

    def test_5_password_change_revokes_existing_sessions(self):
        add_user(type(self).dsn, 'session-owner', 'old-secret')
        status, cookie = self.login('session-owner', 'old-secret')
        self.assertEqual(status, 200)

        add_user(type(self).dsn, 'session-owner', 'new-secret')
        status, _, _ = call('GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(status, 401)
        status, _ = self.login('session-owner', 'new-secret')
        self.assertEqual(status, 200)

    def test_6_passwords_are_hashed_at_rest(self):
        with psycopg.connect(type(self).dsn) as conn:
            rows = conn.execute(
                "SELECT salt, password_hash FROM users WHERE origin = 'local'").fetchall()
        self.assertGreaterEqual(len(rows), 2)
        for salt, password_hash in rows:
            self.assertTrue(salt)
            self.assertRegex(password_hash, r'^pbkdf2_sha256\$600000\$[0-9a-f]{64}$')
            self.assertNotIn(password_hash, (LOCAL_PASSWORD, 's3cret', ''))

    def test_8_external_user_row_and_session_persist_without_password_material(self):
        # docs/auth.md "External authentication": a user verified by the real
        # YTsaurus gets a local row (origin=external, no credentials) whose
        # sessions live in PG like everyone else's.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            f'userdb_pg_{secrets.token_hex(4)}', BACKEND / 'userdb.py')
        userdb = importlib.util.module_from_spec(spec)
        with unittest.mock.patch.dict(os.environ, {
                'MOCK_ENABLE_DEV_SEED_USERS': '',
                'MOCK_PG_DSN': type(self).dsn,
                'MOCK_REQUIRE_AUTH': '1',
        }):
            spec.loader.exec_module(userdb)
        cookie = userdb.external_login('remote-user')
        self.assertTrue(cookie)

        type(self).proc.terminate()
        type(self).proc.wait()
        type(self).proc = start_server(type(self).dsn)  # fresh process, empty RAM
        status, who, _ = call('GET', '/auth/whoami',
                              {'Cookie': f'YTCypressCookie={cookie}'})
        self.assertEqual(status, 200)
        self.assertEqual(who['login'], 'remote-user')

        with psycopg.connect(type(self).dsn) as conn:
            row = conn.execute(
                "SELECT salt, password_hash, origin FROM users WHERE login = 'remote-user'"
            ).fetchone()
        self.assertEqual(row, ('', '', 'external'))
        # Without MOCK_YT_UPSTREAM there is nobody to vouch for this user:
        # no guessable password opens a session.
        self.assertEqual(self.login('remote-user', '')[0], 401)

    def test_9_actions_are_audited_with_strict_fields_and_jsonb_details(self):
        # mock-backend-py/README.md "Audit log": strict columns for
        # ts/login/endpoint/http_code, everything else in schemaless jsonb.
        _, cookie = self.login(LOCAL_USER, LOCAL_PASSWORD)
        self.login(LOCAL_USER, 'wrong-password-audit')
        call('GET', '/auth/whoami', {'Cookie': cookie})
        robot = {'Authorization': 'OAuth persistence-test-robot'}
        call('POST', '/api/v4/get', robot, body={'path': '//home/iceberg/warehouse'})
        call('POST', '/api/v3/execute_batch', robot, body={'requests': [
            {'command': 'get', 'parameters': {'path': '//home'}},
            {'command': 'exists', 'parameters': {'path': '//tmp'}}]})
        call('GET', '/ping')

        with psycopg.connect(type(self).dsn) as conn:
            rows = conn.execute(
                'SELECT ts, login, endpoint, http_code, details'
                ' FROM audit_log ORDER BY id').fetchall()

        def last(endpoint):
            return next(r for r in reversed(rows) if r[2] == endpoint)

        self.assertNotIn('/ping', [r[2] for r in rows])  # probes are exempt

        ts, login, _, http_code, details = last('/api/v4/get')
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(login, 'iceberg')
        self.assertEqual((details['method'], http_code), ('POST', 200))
        self.assertEqual((details['command'], details['path']),
                         ('get', '//home/iceberg/warehouse'))

        _, _, _, _, details = last('/api/v3/execute_batch')
        self.assertEqual(details['requests'], [
            {'command': 'get', 'path': '//home'},
            {'command': 'exists', 'path': '//tmp'}])

        _, login, _, http_code, details = last('/login')  # the failed attempt was last
        self.assertEqual(login, LOCAL_USER)
        self.assertEqual((details['outcome'], http_code), ('rejected', 401))
        success = next(r[4] for r in reversed(rows)
                       if r[2] == '/login' and r[4].get('outcome') == 'success')
        self.assertEqual(success['origin'], 'local')

        self.assertEqual(last('/auth/whoami')[1], LOCAL_USER)
        # Credentials must never reach the audit trail.
        self.assertNotIn('wrong-password-audit', json.dumps([r[4] for r in rows]))

    def test_9b_unexpected_calls_are_audited_too(self):
        # Drift detection depends on this: a UI (or anything else) hitting a
        # route or command we do not serve must leave an attributed trace.
        robot = {'Authorization': 'OAuth persistence-test-robot'}
        call('POST', '/api/v4/frobnicate_table', robot)
        call('GET', '/internal/discover_versions/v2', robot)
        call('POST', '/api/v3/execute_batch', robot, body={'requests': [
            {'command': 'mystery_cmd', 'parameters': {'path': '//x'}}]})

        with psycopg.connect(type(self).dsn) as conn:
            rows = conn.execute(
                'SELECT login, endpoint, http_code, details'
                ' FROM audit_log ORDER BY id').fetchall()

        def last(endpoint):
            return next(r for r in reversed(rows) if r[1] == endpoint)

        login, _, http_code, details = last('/api/v4/frobnicate_table')  # unknown command
        self.assertEqual(login, 'iceberg')
        self.assertEqual((http_code, details['error_code'], details['command']),
                         (404, 1, 'frobnicate_table'))

        login, _, http_code, details = last('/internal/discover_versions/v2')  # unknown route
        self.assertEqual(login, 'iceberg')
        self.assertEqual((http_code, details['error_code']), (404, 1))

        _, _, _, details = last('/api/v3/execute_batch')  # unknown batch item
        self.assertEqual(details['requests'],
                         [{'command': 'mystery_cmd', 'path': '//x'}])

    def test_9c_audit_table_serves_pg_rows_without_details(self):
        # //sys/logs/audit_log surfaces the PG audit trail; the sensitive
        # `details` jsonb is not part of the projection at any layer.
        robot = {'Authorization': 'OAuth persistence-test-robot'}
        call('POST', '/api/v3/get', robot, body={'path': '//home'})
        status, body, _ = call('POST', '/api/v3/read_table', robot, body={
            'path': '//sys/logs/audit_log',
            'output_format': {'$value': 'web_json',
                              '$attributes': {'max_selected_column_count': 50}}})
        self.assertEqual(status, 200)
        self.assertEqual(body['all_column_names'],
                         ['endpoint', 'http_code', 'login', 'ts'])
        self.assertGreater(len(body['rows']), 0)
        self.assertNotIn('details', json.dumps(body))
        row = body['rows'][0]
        self.assertEqual(set(row), {'ts', 'login', 'endpoint', 'http_code'})

    def test_7_database_connection_recovers_after_termination(self):
        _, cookie = self.login(LOCAL_USER, LOCAL_PASSWORD)
        with psycopg.connect(DSN, autocommit=True) as conn:
            terminated = conn.execute(
                'SELECT pg_terminate_backend(pid) FROM pg_stat_activity'
                ' WHERE application_name = %s AND pid <> pg_backend_pid()',
                (type(self).application_name,)).fetchall()
        self.assertTrue(terminated)
        self.assertTrue(all(row[0] for row in terminated))

        for _ in range(20):
            status, _, _ = call('GET', '/ready')
            if status == 200:
                break
            time.sleep(0.1)
        self.assertEqual(status, 200)
        status, who, _ = call(
            'GET', '/auth/whoami', {'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(who['login'], LOCAL_USER)


if __name__ == '__main__':
    unittest.main(verbosity=2)
