#!/usr/bin/env python3
"""External authentication tests (docs/auth.md "External authentication").

A fake "real YTsaurus" /login runs in this process; two mock backends run as
subprocesses — one pointed at it, one pointed at a dead port. Asserted model:
  - users not added locally are verified against MOCK_YT_UPSTREAM and
    provisioned into the local user store on first success (origin=external);
  - locally-added users (test users) authenticate locally and NEVER reach the
    external YTsaurus, even with a wrong password;
  - upstream 5xx or unreachability maps to 503, never to a silent 401.

Run: python3 tests/test_external_auth.py
"""
import base64
import collections
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PORT = 8071          # backend with a live upstream
DEAD_PORT = 8072     # backend whose upstream is unreachable
UPSTREAM_PORT = 8073
UNUSED_PORT = 8074   # nothing listens here

_procs = []
_upstream = None
_redirect_target = None


class RedirectTarget(BaseHTTPRequestHandler):
    """A hostile redirect target that would receive forwarded credentials."""
    hits = []

    def log_message(self, *args):
        pass

    def _respond(self):
        self.hits.append(self.headers.get('Authorization'))
        self.send_response(200)
        self.send_header('Set-Cookie', 'YTCypressCookie=redirected-cookie; Path=/')
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_GET = _respond
    do_POST = _respond


class FakeUpstream(BaseHTTPRequestHandler):
    """Real-YTsaurus stand-in: /login accepts `accounts`, 500s for 'outage'."""
    accounts = {'alice': 'wonderland', 'iceberg': 'real-yt-password'}
    hits = collections.Counter()
    redirect_url = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        if urlsplit(self.path).path != '/login':
            self.send_response(404)
            self.send_header('Content-Length', '0')
            return self.end_headers()
        user = password = None
        auth = self.headers.get('Authorization') or ''
        if auth.startswith('Basic '):
            user, _, password = base64.b64decode(auth[6:]).decode().partition(':')
        FakeUpstream.hits[user] += 1
        if user == 'outage':
            self.send_response(500)
        elif user == 'no-cookie':
            self.send_response(200)
        elif user == 'redirect':
            self.send_response(302)
            self.send_header('Location', self.redirect_url)
        elif self.accounts.get(user) == password:
            self.send_response(200)
            self.send_header('Set-Cookie', 'YTCypressCookie=upstream-cookie; Path=/')
        else:
            self.send_response(401)
        self.send_header('Content-Length', '0')
        self.end_headers()


def setUpModule():
    global _redirect_target, _upstream
    _redirect_target = ThreadingHTTPServer(('localhost', 0), RedirectTarget)
    FakeUpstream.redirect_url = (
        f'http://127.0.0.1:{_redirect_target.server_port}/redirect-target')
    threading.Thread(target=_redirect_target.serve_forever, daemon=True).start()

    _upstream = ThreadingHTTPServer(('localhost', UPSTREAM_PORT), FakeUpstream)
    threading.Thread(target=_upstream.serve_forever, daemon=True).start()

    env = {k: v for k, v in os.environ.items()
           if k not in ('MOCK_PG_DSN', 'MOCK_REQUIRE_AUTH', 'MOCK_ROBOT_TOKEN')}
    for port, upstream_port in ((PORT, UPSTREAM_PORT), (DEAD_PORT, UNUSED_PORT)):
        _procs.append(subprocess.Popen(
            [sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(port)],
            env={**env, 'MOCK_YT_UPSTREAM': f'http://localhost:{upstream_port}',
                 'MOCK_YT_UPSTREAM_TIMEOUT': '2'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for port in (PORT, DEAD_PORT):
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
    for p in _procs:
        p.wait(timeout=10)
    _upstream.shutdown()
    _redirect_target.shutdown()


def call(port, method, path, headers=None):
    req = urllib.request.Request(f'http://localhost:{port}{path}',
                                 headers=dict(headers or {}), method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        raw, status, rh = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as e:
        raw, status, rh = e.read(), e.code, e.headers
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode('utf-8', 'replace')
    return status, parsed, rh


def login(port, user, password):
    creds = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return call(port, 'POST', '/login', {'Authorization': f'Basic {creds}'})


def cookie_of(headers):
    set_cookie = headers.get('Set-Cookie') or ''
    self_pair = set_cookie.split(';', 1)[0]
    name, _, value = self_pair.partition('=')
    return name, value


class TestExternalAuth(unittest.TestCase):
    def test_1_external_user_logs_in_and_is_provisioned(self):
        # First login: verified by the external YTsaurus, then a LOCAL session
        # cookie is issued (the upstream's own cookie is never forwarded).
        status, _, headers = login(PORT, 'alice', 'wonderland')
        self.assertEqual(status, 200)
        name, value = cookie_of(headers)
        self.assertEqual(name, 'YTCypressCookie')
        self.assertNotEqual(value, 'upstream-cookie')
        self.assertEqual(len(value), 64)

        status, body, _ = call(PORT, 'GET', '/auth/whoami',
                               {'Cookie': f'YTCypressCookie={value}'})
        self.assertEqual(status, 200)
        self.assertEqual(body['login'], 'alice')
        self.assertEqual(body['realm'], 'cypress_cookie')
        self.assertTrue(body['csrf_token'])

    def test_2_external_session_passes_csrf_for_mutating_commands(self):
        _, _, headers = login(PORT, 'alice', 'wonderland')
        _, value = cookie_of(headers)
        _, whoami, _ = call(PORT, 'GET', '/auth/whoami',
                            {'Cookie': f'YTCypressCookie={value}'})
        status, body, _ = call(
            PORT, 'POST', '/api/v4/get?path=//home',
            {'Cookie': f'YTCypressCookie={value}', 'X-Csrf-Token': whoami['csrf_token']})
        self.assertEqual(status, 200)
        self.assertIn('value', body)

    def test_3_every_login_reverifies_identity_upstream(self):
        # The local row caches identity, not the password: a password that the
        # external YTsaurus stops accepting stops working here immediately.
        before = FakeUpstream.hits['alice']
        self.assertEqual(login(PORT, 'alice', 'wonderland')[0], 200)
        self.assertEqual(login(PORT, 'alice', 'wonderland')[0], 200)
        self.assertEqual(FakeUpstream.hits['alice'], before + 2)

        status, body, headers = login(PORT, 'alice', 'stale-password')
        self.assertEqual(status, 401)
        self.assertEqual(body['code'], 1)
        self.assertEqual(body['message'], 'Incorrect login or password')
        self.assertEqual(headers.get('WWW-Authenticate'), 'Basic')

    def test_4_unknown_everywhere_is_a_plain_401(self):
        status, body, _ = login(PORT, 'mallory', 'guess')
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], 'Incorrect login or password')

    def test_5_local_test_user_never_reaches_upstream(self):
        # 'iceberg' exists both locally (seed) and upstream with a DIFFERENT
        # password. Local wins and upstream is not consulted — so the local
        # password works, the upstream one does not, and the hit count stays 0.
        self.assertEqual(login(PORT, 'iceberg', 'iceberg')[0], 200)
        self.assertEqual(login(PORT, 'iceberg', 'real-yt-password')[0], 401)
        self.assertEqual(FakeUpstream.hits['iceberg'], 0)

    def test_6_upstream_5xx_maps_to_503(self):
        status, body, _ = login(PORT, 'outage', 'any')
        self.assertEqual(status, 503)
        self.assertEqual(body['message'], 'External authentication is unavailable')

    def test_7_unreachable_upstream_maps_to_503_but_local_users_still_work(self):
        status, body, _ = login(DEAD_PORT, 'alice', 'wonderland')
        self.assertEqual(status, 503)
        self.assertEqual(body['message'], 'External authentication is unavailable')

        self.assertEqual(login(DEAD_PORT, 'iceberg', 'iceberg')[0], 200)
        # Wrong local password fails fast with 401, not 503: local users are
        # decided locally even while the external YTsaurus is down.
        self.assertEqual(login(DEAD_PORT, 'iceberg', 'nope')[0], 401)

    def test_8_upstream_success_without_cookie_maps_to_503(self):
        status, body, _ = login(PORT, 'no-cookie', 'any')
        self.assertEqual(status, 503)
        self.assertEqual(body['message'], 'External authentication is unavailable')

    def test_9_cross_origin_redirect_is_not_followed(self):
        before = list(RedirectTarget.hits)
        status, body, _ = login(PORT, 'redirect', 'secret')
        self.assertEqual(status, 503)
        self.assertEqual(body['message'], 'External authentication is unavailable')
        self.assertEqual(RedirectTarget.hits, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
