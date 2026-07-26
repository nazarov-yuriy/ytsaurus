#!/usr/bin/env python3
"""Cypress cookie-model and CSRF-construction tests, ported from the YTsaurus
suites (test_cypress_cookie_auth.py, helpers_ut.cpp TTestCsrfTokenTest) and run
against BOTH backends with tuned TTLs:

- cookie value format: 64 hex chars (GenerateCookieValue parity)
- //sys/cypress_cookies/<value>[/<field>] virtual store view
- cookie rotation near expiry; the old cookie stays valid until it expires
- CSRF token: hex(hmac_sha256(secret, "user:ts")) + ":" + ts, tamper-rejected
  with the real NRpc code 110

Run: python3 tests/test_cookie_model.py
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTS = {'node': 8051, 'python': 8052}
_only = os.environ.get('BACKEND')
BACKENDS = {k: v for k, v in PORTS.items() if not _only or k == _only}

TTL, RENEWAL = 4, 3  # rotate when < 3s of the 4s TTL remain (i.e. after ~1s)
_procs = []


def setUpModule():
    env = {**{k: v for k, v in os.environ.items() if k != 'MOCK_PG_DSN'},
           'MOCK_COOKIE_TTL_SECONDS': str(TTL),
           'MOCK_COOKIE_RENEWAL_SECONDS': str(RENEWAL),
           'MOCK_CSRF_SECRET': 'cookie-model-test-secret'}
    if 'node' in BACKENDS:
        _procs.append(subprocess.Popen(['node', str(ROOT / 'mock-backend' / 'server.js'),
                                        str(PORTS['node'])], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if 'python' in BACKENDS:
        _procs.append(subprocess.Popen([sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'),
                                        str(PORTS['python'])], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for port in BACKENDS.values():
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


def login(port):
    _, _, hdrs = call(port, 'POST', '/login', headers={
        'Authorization': 'Basic ' + base64.b64encode(b'iceberg:iceberg').decode()})
    return hdrs['Set-Cookie'].split(';')[0].split('=', 1)[1]


class Both(unittest.TestCase):
    def each(self):
        for name, port in BACKENDS.items():
            with self.subTest(backend=name):
                yield port


class TestCookieFormat(Both):
    def test_value_is_64_hex(self):
        # test_cookie_format / GenerateCookieValue (cypress_cookie.cpp:47-53)
        for port in self.each():
            value = login(port)
            self.assertRegex(value, r'^[0-9a-f]{64}$')


class TestCypressCookieStore(Both):
    def test_cookie_visible_in_cypress_store(self):
        # test_cookie_in_cypress: get //sys/cypress_cookies/<cookie>/{value,user}
        for port in self.each():
            value = login(port)
            _, got, _ = call(port, 'POST', '/api/v3/get',
                             body={'path': f'//sys/cypress_cookies/{value}/value'})
            self.assertEqual(got, value)
            _, user, _ = call(port, 'POST', '/api/v3/get',
                              body={'path': f'//sys/cypress_cookies/{value}/user'})
            self.assertEqual(user, 'iceberg')
            _, record, _ = call(port, 'POST', '/api/v3/get',
                                body={'path': f'//sys/cypress_cookies/{value}'})
            self.assertEqual(record['auth_source'], 'password')
            self.assertIn('expires_at', record)
            _, listing, _ = call(port, 'POST', '/api/v3/list',
                                 body={'path': '//sys/cypress_cookies'})
            self.assertIn(value, listing)

    def test_unknown_cookie_resolves_as_error(self):
        for port in self.each():
            status, body, _ = call(port, 'POST', '/api/v3/get',
                                   body={'path': '//sys/cypress_cookies/' + '0' * 64})
            self.assertEqual(status, 400)
            self.assertEqual(body['code'], 500)


class TestCookieRotation(Both):
    def test_rotation_and_old_cookie_validity(self):
        # test_cookie_rotation: once inside the renewal window an authenticated
        # request yields a fresh Set-Cookie; the old cookie stays valid meanwhile.
        for port in self.each():
            value = login(port)
            _, _, hdrs = call(port, 'GET', '/auth/whoami',
                              headers={'Cookie': f'YTCypressCookie={value}'})
            self.assertIsNone(hdrs.get('Set-Cookie'))  # too fresh to rotate

            time.sleep(TTL - RENEWAL + 0.3)  # enter the renewal window
            _, who, hdrs = call(port, 'GET', '/auth/whoami',
                                headers={'Cookie': f'YTCypressCookie={value}'})
            self.assertEqual(who['realm'], 'cypress_cookie')
            new_value = re.search(r'YTCypressCookie=([0-9a-f]{64})', hdrs.get('Set-Cookie', ''))
            self.assertIsNotNone(new_value)
            self.assertNotEqual(new_value.group(1), value)

            # old cookie still authenticates until its expiry...
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={value}'})
            self.assertEqual(who['realm'], 'cypress_cookie')
            # ...and stops afterwards (anonymous fallback realm in default mode)
            time.sleep(RENEWAL + 0.3)
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={value}'})
            self.assertEqual(who['realm'], 'mock')
            # the rotated cookie was issued later and still works
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={new_value.group(1)}'})
            self.assertEqual(who['realm'], 'cypress_cookie')


class TestCsrfConstruction(Both):
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

    def test_tampered_and_malformed_tokens_rejected_with_code_110(self):
        for port in self.each():
            value = login(port)
            _, who, _ = call(port, 'GET', '/auth/whoami',
                             headers={'Cookie': f'YTCypressCookie={value}'})
            sig, ts = who['csrf_token'].split(':')
            tampered = ('0' * 64) + ':' + ts
            for token, expected_message in (
                    (tampered, 'Invalid CSFR token signature'),  # typo as in helpers.cpp
                    ('garbage', 'Malformed CSRF token'),
                    (sig + ':1', 'CSRF token expired')):
                status, body, _ = call(
                    port, 'POST', '/api/v3/exists', body={'path': '//tmp'},
                    headers={'Cookie': f'YTCypressCookie={value}', 'X-Csrf-Token': token})
                self.assertEqual(status, 401, token)
                self.assertEqual(body['code'], 110)
                self.assertEqual(body['message'], expected_message)


if __name__ == '__main__':
    unittest.main(verbosity=2)
