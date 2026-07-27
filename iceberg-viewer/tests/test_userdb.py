#!/usr/bin/env python3
"""Focused tests for password hashing and PostgreSQL connection recovery."""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
USERDB = ROOT / 'mock-backend-py' / 'userdb.py'


class FakePsycopgError(Exception):
    pass


class FakeOperationalError(FakePsycopgError):
    pass


class FakeInterfaceError(FakePsycopgError):
    pass


class FakeResult:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, fail_first_list=False, fail_statement=None):
        self.closed = False
        self.fail_first_list = fail_first_list
        self.fail_statement = fail_statement

    def execute(self, statement, params=()):
        if statement == self.fail_statement:
            self.fail_statement = None
            raise FakeOperationalError('connection was lost during a mutation')
        if statement == 'SELECT login, origin FROM users ORDER BY login':
            if self.fail_first_list:
                self.fail_first_list = False
                raise FakeOperationalError('connection was lost')
            return FakeResult([('alice', 'local')])
        return FakeResult()

    def close(self):
        self.closed = True


def fake_psycopg(connect):
    module = types.ModuleType('psycopg')
    module.Error = FakePsycopgError
    module.OperationalError = FakeOperationalError
    module.InterfaceError = FakeInterfaceError
    module.connect = connect
    return module


def load_userdb(
        dsn='', psycopg_module=None, require_auth=False,
        dev_seed_users=False):
    name = f'test_userdb_{uuid.uuid4().hex}'
    spec = importlib.util.spec_from_file_location(name, USERDB)
    module = importlib.util.module_from_spec(spec)
    modules = {'psycopg': psycopg_module} if psycopg_module else {}
    environment = {
        'MOCK_ENABLE_DEV_SEED_USERS': '1' if dev_seed_users else '',
        'MOCK_PG_DSN': dsn,
        'MOCK_REQUIRE_AUTH': '1' if require_auth else '',
    }
    with mock.patch.dict(os.environ, environment):
        with mock.patch.dict(sys.modules, modules):
            spec.loader.exec_module(module)
    return module


class TestPasswordStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.userdb = load_userdb()

    def test_new_passwords_use_pbkdf2(self):
        self.userdb.set_password('alice', 's3cret')
        salt, password_hash = self.userdb._users['alice']
        scheme, iterations, digest = password_hash.split('$')

        self.assertEqual(scheme, 'pbkdf2_sha256')
        self.assertEqual(int(iterations), self.userdb.PBKDF2_ITERATIONS)
        self.assertEqual(len(salt), 32)
        self.assertEqual(len(digest), 64)
        self.assertTrue(self.userdb.verify('alice', 's3cret'))
        self.assertFalse(self.userdb.verify('alice', 'wrong'))

    def test_default_store_has_no_published_development_users(self):
        self.assertNotIn('iceberg', self.userdb.list_users())
        self.assertNotIn('root', self.userdb.list_users())
        self.assertFalse(self.userdb.verify('iceberg', 'iceberg'))
        self.assertFalse(self.userdb.verify('root', ''))

    def test_cli_rejects_passwords_in_process_arguments(self):
        environment = {
            **os.environ,
            'MOCK_ENABLE_DEV_SEED_USERS': '',
            'MOCK_PG_DSN': '',
            'MOCK_REQUIRE_AUTH': '',
        }
        exposed = subprocess.run(
            [
                sys.executable,
                str(USERDB),
                'add-user',
                'alice',
                'password-in-argv',
            ],
            env=environment, capture_output=True, text=True, check=False)
        self.assertNotEqual(exposed.returncode, 0)
        self.assertIn('--password-stdin', exposed.stderr)

        protected = subprocess.run(
            [
                sys.executable,
                str(USERDB),
                'add-user',
                'alice',
                '--password-stdin',
            ],
            env=environment, input='password-over-stdin\n',
            capture_output=True, text=True, check=False)
        self.assertEqual(protected.returncode, 0, protected.stderr)
        self.assertNotIn('password-over-stdin', protected.stdout)

    def test_development_seeds_require_explicit_anonymous_opt_in(self):
        development = load_userdb(dev_seed_users=True)
        self.assertTrue(development.verify('iceberg', 'iceberg'))
        self.assertTrue(development.verify('root', ''))

        authenticated = load_userdb(
            require_auth=True, dev_seed_users=True)
        self.assertEqual(authenticated.list_users(), [])
        self.assertFalse(authenticated.verify('iceberg', 'iceberg'))
        self.assertFalse(authenticated.verify('root', ''))

    def test_authenticated_store_rejects_only_published_password_pairs(self):
        authenticated = load_userdb(require_auth=True)
        authenticated.set_password('local-test', 'local-secret')
        authenticated.set_password('iceberg', 'strong-iceberg-secret')
        authenticated.set_password('root', 'strong-root-secret')

        self.assertIsNotNone(authenticated.authenticate_and_create_session(
            'local-test', 'local-secret'))
        self.assertFalse(authenticated.verify('iceberg', 'iceberg'))
        self.assertFalse(authenticated.verify('root', ''))
        self.assertTrue(
            authenticated.verify('iceberg', 'strong-iceberg-secret'))
        self.assertTrue(authenticated.verify('root', 'strong-root-secret'))
        # Non-ASCII passwords for published login names must compare False,
        # not raise (compare_digest rejects non-ASCII str).
        self.assertFalse(
            authenticated.is_published_development_credential('iceberg', 'пароль'))
        self.assertFalse(authenticated.verify('iceberg', 'пароль'))

    def test_ram_store_is_always_healthy(self):
        self.assertTrue(self.userdb.healthy())

    def test_password_change_revokes_existing_sessions(self):
        self.userdb.set_password('session-owner', 'old-secret')
        cookie = self.userdb.create_session('session-owner')
        self.assertEqual(self.userdb.session_user(cookie), 'session-owner')

        self.userdb.set_password('session-owner', 'new-secret')
        self.assertIsNone(self.userdb.session_user(cookie))
        self.assertFalse(self.userdb.verify('session-owner', 'old-secret'))
        self.assertTrue(self.userdb.verify('session-owner', 'new-secret'))

    def test_password_change_cannot_leave_racing_login_authenticated(self):
        self.userdb.set_password('racing-owner', 'old-secret')
        password_checked = threading.Event()
        allow_login_to_finish = threading.Event()
        original_matches = self.userdb._password_matches

        def paused_password_matches(password, salt, password_hash):
            matched = original_matches(password, salt, password_hash)
            password_checked.set()
            allow_login_to_finish.wait(timeout=5)
            return matched

        login_result = []
        password_change_started = threading.Event()

        def login():
            login_result.append(
                self.userdb.authenticate_and_create_session(
                    'racing-owner', 'old-secret'))

        def change_password():
            password_change_started.set()
            self.userdb.set_password('racing-owner', 'new-secret')

        with mock.patch.object(
                self.userdb, '_password_matches', side_effect=paused_password_matches):
            login_thread = threading.Thread(target=login)
            login_thread.start()
            self.assertTrue(password_checked.wait(timeout=5))

            password_thread = threading.Thread(target=change_password)
            password_thread.start()
            self.assertTrue(password_change_started.wait(timeout=5))
            allow_login_to_finish.set()
            login_thread.join(timeout=5)
            password_thread.join(timeout=5)

        self.assertFalse(login_thread.is_alive())
        self.assertFalse(password_thread.is_alive())
        self.assertEqual(len(login_result), 1)
        self.assertIsNotNone(login_result[0])
        self.assertIsNone(self.userdb.session_user(login_result[0]))
        self.assertFalse(self.userdb.verify('racing-owner', 'old-secret'))
        self.assertTrue(self.userdb.verify('racing-owner', 'new-secret'))

    def test_external_login_provisions_user_without_password_material(self):
        # docs/auth.md "External authentication": the row exists for sessions
        # and settings, but no credential ever verifies locally.
        cookie = self.userdb.external_login('eve')
        self.assertEqual(self.userdb.session_user(cookie), 'eve')
        self.assertEqual(self.userdb.user_origin('eve'), 'external')
        self.assertFalse(self.userdb.verify('eve', ''))
        self.assertFalse(self.userdb.verify('eve', 'anything'))
        self.assertIsNone(self.userdb.authenticate_and_create_session('eve', ''))

    def test_external_login_never_shadows_a_local_user(self):
        self.userdb.set_password('local-only', 'pw')
        self.assertIsNone(self.userdb.external_login('local-only'))

    def test_set_password_converts_external_user_to_local(self):
        self.userdb.external_login('promoted')
        self.userdb.set_password('promoted', 'pw')
        self.assertEqual(self.userdb.user_origin('promoted'), 'local')
        self.assertTrue(self.userdb.verify('promoted', 'pw'))
        self.assertIsNone(self.userdb.external_login('promoted'))

    def test_audit_log_keeps_strict_essentials_and_free_form_details(self):
        # Strict fields: timestamp, user, endpoint. Everything else is an
        # arbitrary details payload whose shape is expected to change.
        self.userdb.audit('alice', '/api/v4/get',
                          {'command': 'get', 'path': '//home', 'status': 200})
        self.userdb.audit(None, '/login', {'outcome': 'rejected', 'novel_field': [1, {'x': 2}]})
        first, second = self.userdb.audit_tail(2)

        ts, login, endpoint, details = first
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual((login, endpoint), ('alice', '/api/v4/get'))
        self.assertEqual(details['path'], '//home')
        ts, login, endpoint, details = second
        self.assertIsNone(login)  # unauthenticated actions carry no user
        self.assertEqual(endpoint, '/login')
        self.assertEqual(details['novel_field'], [1, {'x': 2}])

    def test_audit_payload_is_bounded_and_batch_requests_are_summarised(self):
        requests = [
            {'command': f'command-{index}-' + ('c' * 500),
             'path': f'//huge/path/{index}/' + ('p' * 2_000),
             # A future call site must not accidentally log whole requests.
             'parameters': {'body': 'secret-request-payload' * 500}}
            for index in range(100)
        ]
        self.userdb.audit(
            'login-' + ('u' * 10_000),
            '/api/v3/execute_batch/' + ('e' * 10_000),
            {'method': 'POST', 'status': 200, 'command': 'execute_batch',
             'requests': requests, 'unbounded': 'x' * 100_000})
        _, login, endpoint, details = self.userdb.audit_tail(1)[0]

        payload = {'login': login, 'endpoint': endpoint, 'details': details}
        self.assertLess(
            len(json.dumps(
                payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')),
            self.userdb.AUDIT_PAYLOAD_LIMIT_BYTES)
        self.assertTrue(login.endswith('\u2026'))
        self.assertTrue(endpoint.endswith('\u2026'))
        self.assertEqual(
            (details['method'], details['status'], details['command']),
            ('POST', 200, 'execute_batch'))
        self.assertTrue(details['_audit_truncated'])
        self.assertLess(len(details['requests']), len(requests))
        self.assertEqual(
            details['requests_omitted'],
            len(requests) - len(details['requests']))
        self.assertNotIn('parameters', details['requests'][0])
        self.assertNotIn(
            'secret-request-payload',
            self.userdb._compact_json(payload))

    def test_audit_stores_a_detached_sanitised_copy(self):
        details = {'path': '//tmp/\x00object', 'values': ['before']}
        self.userdb.audit('user\x00name', '/api/\x00get', details)
        details['values'][0] = 'after'
        _, login, endpoint, stored = self.userdb.audit_tail(1)[0]

        self.assertNotIn('\x00', login + endpoint + stored['path'])
        self.assertEqual(stored['values'], ['before'])

    def test_audit_redacts_nested_credentials_and_keeps_benign_fields(self):
        secret_values = [
            'plain-password',
            'basic-authorization',
            'session-cookie',
            'csrf-token-value',
            'oauth-access-token',
            'oauth-refresh-token',
            'oidc-id-token',
            'robot-token-value',
            'client-secret-value',
            'database-password',
            'api-secret-value',
            'cloud-credential-value',
            'api-key-value',
            'private-key-value',
            'session-value',
        ]
        details = {
            'password': secret_values[0],
            'headers': {
                'Authorization': secret_values[1],
                'Cookie': secret_values[2],
                'X-CSRF-Token': secret_values[3],
            },
            'oauth': {
                'access_token': secret_values[4],
                'refreshToken': secret_values[5],
                'id-token': secret_values[6],
                'robot_token': secret_values[7],
                'clientSecret': secret_values[8],
                'apiKey': secret_values[12],
                'private_key': secret_values[13],
                'session': secret_values[14],
            },
            'database_password': secret_values[9],
            'api-secret': secret_values[10],
            'cloud_credential': secret_values[11],
            'metadata': {
                'error_code': 401,
                'path': '//home',
                'command': 'get',
                'primary_key': 'id',
                'token_count': 12,
            },
        }

        self.userdb.audit('alice', '/api/v4/get', details)
        details['headers']['Authorization'] = 'changed-after-audit'
        _, _, _, stored = self.userdb.audit_tail(1)[0]
        encoded = self.userdb._compact_json(stored)

        for secret_value in [*secret_values, 'changed-after-audit']:
            self.assertNotIn(secret_value, encoded)
        self.assertEqual(stored['password'], '<redacted>')
        self.assertEqual(stored['headers']['Authorization'], '<redacted>')
        self.assertEqual(stored['oauth']['refreshToken'], '<redacted>')
        self.assertEqual(stored['database_password'], '<redacted>')
        self.assertEqual(stored['api-secret'], '<redacted>')
        self.assertEqual(stored['cloud_credential'], '<redacted>')
        self.assertEqual(stored['oauth']['apiKey'], '<redacted>')
        self.assertEqual(stored['oauth']['private_key'], '<redacted>')
        self.assertEqual(stored['oauth']['session'], '<redacted>')
        self.assertEqual(
            {key: stored['metadata'][key] for key in (
                'error_code', 'path', 'command', 'primary_key', 'token_count')},
            {'error_code': 401, 'path': '//home', 'command': 'get',
             'primary_key': 'id', 'token_count': 12})
        self.assertTrue(stored['_audit_truncated'])
        self.assertLess(
            self.userdb._json_size({
                'login': 'alice', 'endpoint': '/api/v4/get', 'details': stored}),
            self.userdb.AUDIT_PAYLOAD_LIMIT_BYTES)

    def test_audit_normalisation_has_bounded_depth_and_width(self):
        class NoFullIteration(list):
            def __iter__(self):
                raise AssertionError('audit traversed the full list')

        deep = 'leaf'
        for _ in range(5_000):
            deep = {'next': deep}
        wide = NoFullIteration(range(100_000))

        self.userdb.audit('alice', '/api/v4/get', {
            'method': 'POST', 'deep': deep, 'wide': wide,
            'huge_text': 'x' * 1_000_000})
        _, _, _, stored = self.userdb.audit_tail(1)[0]

        self.assertTrue(stored['_audit_truncated'])
        self.assertEqual(
            stored['deep']['next']['next'], {'item_count': 1})
        self.assertEqual(stored['wide'], list(range(8)))
        self.assertTrue(stored['huge_text'].endswith('\u2026'))
        self.assertLess(
            self.userdb._json_size({
                'login': 'alice', 'endpoint': '/api/v4/get', 'details': stored}),
            self.userdb.AUDIT_PAYLOAD_LIMIT_BYTES)

    def test_malformed_or_excessive_pbkdf2_hashes_fail_closed(self):
        malformed_hashes = [
            'pbkdf2_sha256$not-a-number$00',
            'pbkdf2_sha256$600000$not-hex',
            self.userdb._hash('secret', 'salt', 1),
            'pbkdf2_sha256$5000001$' + ('00' * 32),
            '0' * 64,
            None,
        ]
        for index, password_hash in enumerate(malformed_hashes):
            with self.subTest(password_hash=password_hash):
                login = f'broken-{index}'
                self.userdb._users[login] = ('salt', password_hash)
                self.assertFalse(self.userdb.verify(login, 'secret'))


class TestPostgresRecovery(unittest.TestCase):
    def test_authenticated_initialization_never_inserts_development_seeds(self):
        connections = []

        class RecordingConnection(FakeConnection):
            def __init__(self):
                super().__init__()
                self.executions = []

            def execute(self, statement, params=()):
                self.executions.append((statement, params))
                return super().execute(statement, params)

        def connect(_dsn, autocommit):
            connection = RecordingConnection()
            connections.append(connection)
            return connection

        userdb = load_userdb(
            'dbname=mock', fake_psycopg(connect),
            require_auth=True, dev_seed_users=True)
        self.assertEqual(userdb.list_users(), ['alice'])
        self.assertFalse(any(
            statement.startswith('INSERT INTO users')
            for statement, _ in connections[0].executions))

    def test_postgres_audit_uses_the_same_bounded_payload(self):
        connections = []

        class RecordingConnection(FakeConnection):
            def __init__(self):
                super().__init__()
                self.executions = []

            def execute(self, statement, params=()):
                self.executions.append((statement, params))
                return super().execute(statement, params)

        def connect(_dsn, autocommit):
            connection = RecordingConnection()
            connections.append(connection)
            return connection

        userdb = load_userdb('dbname=mock', fake_psycopg(connect))
        userdb.audit(
            'u' * 10_000,
            '/unknown/' + ('p' * 10_000),
            {'path': '//home',
             'nested': {
                 'accessToken': 'postgres-access-secret',
                 'client_secret': 'postgres-client-secret',
                 'error_code': 'benign-error-code'}})
        _, params = next(
            execution for execution in connections[0].executions
            if execution[0].startswith('INSERT INTO audit_log'))
        login, endpoint, raw_details = params
        details = json.loads(raw_details)

        self.assertNotIn('postgres-access-secret', raw_details)
        self.assertNotIn('postgres-client-secret', raw_details)
        self.assertEqual(details['nested']['accessToken'], '<redacted>')
        self.assertEqual(details['nested']['client_secret'], '<redacted>')
        self.assertEqual(details['nested']['error_code'], 'benign-error-code')
        self.assertLess(
            len(json.dumps(
                {'login': login, 'endpoint': endpoint, 'details': details},
                ensure_ascii=False, separators=(',', ':')).encode('utf-8')),
            userdb.AUDIT_PAYLOAD_LIMIT_BYTES)

    def test_mutation_connection_loss_is_not_replayed(self):
        connections = []

        def connect(_dsn, autocommit):
            connection = FakeConnection(
                fail_statement='UPDATE users SET password_hash = %s'
                if not connections else None)
            connections.append(connection)
            return connection

        userdb = load_userdb('dbname=mock', fake_psycopg(connect))
        with self.assertRaises(FakeOperationalError):
            userdb._query(
                'UPDATE users SET password_hash = %s',
                ('new-hash',), retry=False)
        self.assertEqual(len(connections), 1)
        self.assertTrue(connections[0].closed)

        # The following read opens a fresh connection and succeeds.
        self.assertEqual(userdb.list_users(), ['alice'])
        self.assertEqual(len(connections), 2)

    def test_query_reconnects_once_after_connection_loss(self):
        connections = []

        def connect(_dsn, autocommit):
            self.assertTrue(autocommit)
            connection = FakeConnection(fail_first_list=not connections)
            connections.append(connection)
            return connection

        userdb = load_userdb('dbname=mock', fake_psycopg(connect))
        self.assertEqual(connections, [])  # connecting is lazy
        self.assertEqual(userdb.list_users(), ['alice'])
        self.assertEqual(len(connections), 2)
        self.assertTrue(connections[0].closed)
        self.assertFalse(connections[1].closed)

    def test_health_check_reports_database_outage_after_one_retry(self):
        attempts = []

        def connect(_dsn, autocommit):
            attempts.append(autocommit)
            raise FakeOperationalError('database unavailable')

        userdb = load_userdb('dbname=mock', fake_psycopg(connect))
        self.assertFalse(userdb.healthy())
        self.assertEqual(attempts, [True, True])


if __name__ == '__main__':
    unittest.main(verbosity=2)
