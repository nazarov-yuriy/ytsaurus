#!/usr/bin/env python3
"""Cypress cookie-model and CSRF-construction tests, ported from the YTsaurus
suites (test_cypress_cookie_auth.py, helpers_ut.cpp TTestCsrfTokenTest) and run
against the Python backend with tuned TTLs:

- cookie value format: 64 hex chars (GenerateCookieValue parity)
- browser expiry and Secure/HttpOnly/Path attributes match the server TTL
- the privileged //sys/cypress_cookies store is not exposed by the mock API
- CSRF token: hex(hmac_sha256(secret, "user:ts")) + ":" + ts, tamper-rejected
  with YTsaurus-compatible status/error distinctions

Run: python3 tests/test_cookie_model.py
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
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8052

TTL = 4
_procs = []


def setUpModule():
    env = {**{k: v for k, v in os.environ.items()
              if k not in (
                  'MOCK_ENABLE_DEV_SEED_USERS',
                  'MOCK_PG_DSN',
                  'MOCK_REQUIRE_AUTH',
              )},
           'MOCK_COOKIE_TTL_SECONDS': str(TTL),
           'MOCK_CSRF_SECRET': 'cookie-model-test-secret',
           'MOCK_ENABLE_DEV_SEED_USERS': '1'}
    _procs.append(subprocess.Popen(
        [sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for port in (PORT,):
        for _ in range(50):
            try:
                urllib.request.urlopen(f'http://localhost:{port}/ping', timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f'backend on :{port} did not start')


def tearDownModule():
    for p in _procs:
        p.terminate()


def call(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data:
        hdrs.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(f'http://localhost:{port}{path}', data=data,
                                 headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        raw, status, rh = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as e:
        raw, status, rh = e.read(), e.code, e.headers
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode()
    return status, parsed, rh


def login_header(port, path='/login'):
    status, _, hdrs = call(port, 'POST', path, headers={
        'Authorization': 'Basic ' + base64.b64encode(b'iceberg:iceberg').decode()})
    return status, hdrs['Set-Cookie']


def login(port, path='/login'):
    _, header = login_header(port, path)
    return header.split(';')[0].split('=', 1)[1]


class BackendTestCase(unittest.TestCase):
    def each(self):
        yield PORT


class TestCookieFormat(BackendTestCase):
    def test_value_and_attributes_match_cookie_config(self):
        # test_cookie_format / GenerateCookieValue / TCypressCookie::ToHeader.
        for port in self.each():
            before = datetime.now(timezone.utc)
            status, header = login_header(port)
            self.assertEqual(status, 200)
            cookie, expires, secure, http_only, path = header.split(';')
            value = cookie.split('=', 1)[1]
            self.assertRegex(value, r'^[0-9a-f]{64}$')
            expiry = parsedate_to_datetime(expires.strip().removeprefix('Expires='))
            self.assertGreaterEqual(expiry, before)
            self.assertLessEqual(
                expiry, datetime.now(timezone.utc) + timedelta(seconds=TTL + 1))
            self.assertEqual(secure, ' Secure')
            self.assertEqual(http_only, ' HttpOnly')
            self.assertEqual(path, ' Path=/')

    def test_login_route_variants_match_real_proxy(self):
        for port in self.each():
            for path in ('/login', '/login/', '/login/foo?bar=a&baz=b'):
                status, header = login_header(port, path)
                self.assertEqual(status, 200, path)
                self.assertIn('YTCypressCookie=', header)


class TestCookieStoreIsolation(BackendTestCase):
    def test_cookie_store_is_not_exposed_to_api_users(self):
        # The real node is privileged; exposing its keys would leak bearer
        # credentials because the mock has no Cypress ACL engine.
        for port in self.each():
            value = login(port)
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={value}'})
            auth_headers = {'Cookie': f'YTCypressCookie={value}',
                            'X-Csrf-Token': who['csrf_token']}
            for command, path in (
                    ('list', '//sys/cypress_cookies'),
                    ('get', '//sys/cypress_cookies'),
                    ('get', f'//sys/cypress_cookies/{value}/value')):
                status, body, _ = call(
                    port, 'POST', f'/api/v3/{command}', body={'path': path},
                    headers=auth_headers)
                self.assertEqual(status, 400, path)
                self.assertEqual(body['code'], 500)

    def test_cookie_expires_server_side(self):
        for port in self.each():
            value = login(port)
            time.sleep(TTL + 0.3)
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={value}'})
            self.assertEqual(who['realm'], 'mock')


class TestCsrfConstruction(BackendTestCase):
    def test_token_format_and_verification(self):
        # SignCsrfToken (auth_server/helpers.cpp:160-164): with a shared secret the
        # signature is reproducible: hex(hmac_sha256(secret, "user:ts")) + ":" + ts.
        import hmac as hmac_mod
        for port in self.each():
            value = login(port)
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={value}'})
            token = who['csrf_token']
            sig, ts = token.split(':')
            expected = hmac_mod.new(b'cookie-model-test-secret',
                                    f'iceberg:{ts}'.encode(), 'sha256').hexdigest()
            self.assertEqual(sig, expected)

            ok = call(port, 'POST', '/api/v3/exists', body={'path': '//tmp'},
                      headers={'Cookie': f'YTCypressCookie={value}', 'X-Csrf-Token': token})
            self.assertEqual(ok[0], 200)

    def test_invalid_token_errors_match_real_status_and_code(self):
        for port in self.each():
            value = login(port)
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={value}'})
            sig, ts = who['csrf_token'].split(':')
            tampered = ('0' * 64) + ':' + ts
            cases = (
                (None, 401, 111, 'CSRF token is missing'),
                (tampered, 401, 110, 'Invalid CSFR token signature'),
                ('garbage', 503, 1, 'Malformed CSRF token'),
                (f'{sig}:{ts}:extra', 503, 1, 'Malformed CSRF token'),
                (sig + ':1', 401, 110, 'CSRF token expired'),
            )
            for token, expected_status, expected_code, expected_message in cases:
                headers = {'Cookie': f'YTCypressCookie={value}'}
                if token is not None:
                    headers['X-Csrf-Token'] = token
                status, body, _ = call(
                    port, 'POST', '/api/v3/exists', body={'path': '//tmp'},
                    headers=headers)
                self.assertEqual(status, expected_status, token)
                self.assertEqual(body['code'], expected_code)
                self.assertEqual(body['message'], expected_message)


if __name__ == '__main__':
    unittest.main(verbosity=2)
