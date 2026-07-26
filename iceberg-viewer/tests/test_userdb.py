#!/usr/bin/env python3
"""Focused tests for password hashing and PostgreSQL connection recovery."""
import hashlib
import importlib.util
import os
import sys
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
        if statement == 'SELECT login FROM users ORDER BY login':
            if self.fail_first_list:
                self.fail_first_list = False
                raise FakeOperationalError('connection was lost')
            return FakeResult([('iceberg',), ('root',)])
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


def load_userdb(dsn='', psycopg_module=None):
    name = f'test_userdb_{uuid.uuid4().hex}'
    spec = importlib.util.spec_from_file_location(name, USERDB)
    module = importlib.util.module_from_spec(spec)
    modules = {'psycopg': psycopg_module} if psycopg_module else {}
    with mock.patch.dict(os.environ, {'MOCK_PG_DSN': dsn}):
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

    def test_legacy_sha256_is_upgraded_after_successful_login(self):
        salt = '0123456789abcdef'
        legacy_hash = hashlib.sha256(f'{salt}:old-secret'.encode()).hexdigest()
        self.userdb._users['legacy'] = (salt, legacy_hash)

        self.assertFalse(self.userdb.verify('legacy', 'wrong'))
        self.assertEqual(self.userdb._users['legacy'], (salt, legacy_hash))
        self.assertTrue(self.userdb.verify('legacy', 'old-secret'))

        new_salt, new_hash = self.userdb._users['legacy']
        self.assertNotEqual(new_salt, salt)
        self.assertTrue(new_hash.startswith('pbkdf2_sha256$600000$'))

    def test_ram_store_is_always_healthy(self):
        self.assertTrue(self.userdb.healthy())

    def test_malformed_or_excessive_pbkdf2_hashes_fail_closed(self):
        malformed_hashes = [
            'pbkdf2_sha256$not-a-number$00',
            'pbkdf2_sha256$600000$not-hex',
            'pbkdf2_sha256$5000001$' + ('00' * 32),
            None,
        ]
        for index, password_hash in enumerate(malformed_hashes):
            with self.subTest(password_hash=password_hash):
                login = f'broken-{index}'
                self.userdb._users[login] = ('salt', password_hash)
                self.assertFalse(self.userdb.verify(login, 'secret'))


class TestPostgresRecovery(unittest.TestCase):
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
        self.assertEqual(userdb.list_users(), ['iceberg', 'root'])
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
        self.assertEqual(userdb.list_users(), ['iceberg', 'root'])
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
