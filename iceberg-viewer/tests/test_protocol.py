#!/usr/bin/env python3
"""Protocol conformance tests for the Python mock YT backend.

Every test asserts *documented* wire behavior — each carries a reference to the
doc that specifies it (docs/auth.md, docs/table-viewer.md, docs/bootstrap-config.md,
docs/empirical-findings.md).

Run: python3 tests/test_protocol.py
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
PORT = 8012
STRICT_PORT = 8014
CORS_PORT = 8015

_procs = []


def setUpModule():
    anonymous_env = {
        key: value for key, value in os.environ.items()
        if key not in (
            'MOCK_CORS_ORIGINS',
            'MOCK_ENABLE_DEV_SEED_USERS',
            'MOCK_REQUIRE_AUTH',
            'MOCK_ROBOT_TOKEN',
        )
    }
    anonymous_env['MOCK_ENABLE_DEV_SEED_USERS'] = '1'
    _procs.append(subprocess.Popen(
        [sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(PORT)],
        env=anonymous_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    strict_env = {
        key: value for key, value in anonymous_env.items()
        if key not in ('MOCK_ENABLE_DEV_SEED_USERS', 'MOCK_PG_DSN')
    }
    strict_env.update({
        'MOCK_REQUIRE_AUTH': '1',
        'MOCK_ROBOT_TOKEN': 'protocol-test-robot',
    })
    _procs.append(subprocess.Popen(
        [sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(STRICT_PORT)],
        env=strict_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    cors_env = {
        **anonymous_env,
        'MOCK_CORS_ORIGINS': 'https://viewer.internal',
    }
    _procs.append(subprocess.Popen(
        [sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(CORS_PORT)],
        env=cors_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for port in (PORT, STRICT_PORT, CORS_PORT):
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


def call(port, method, path, body=None, headers=None):
    """Send one request; return (status, parsed_json_or_text, headers)."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
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


class BackendTestCase(unittest.TestCase):
    """Base class for tests against the normal backend process."""

    def each(self):
        yield PORT


class TestInfrastructure(BackendTestCase):
    def test_readiness_endpoint_is_public(self):
        for port in self.each():
            status, body, _ = call(port, 'GET', '/ready')
            self.assertEqual(status, 200)
            self.assertEqual(body, {})

    def test_version_matches_ui_regex(self):
        # bootstrap-config.md §6: the UI extracts /(\d+)\.(\d+)\.(\d+)/ from the
        # plain-text /version body; no match => PRELOAD_ERROR.CONNECTION, page never mounts.
        import re
        for port in self.each():
            status, body, hdrs = call(port, 'GET', '/version')
            self.assertEqual(status, 200)
            self.assertRegex(body, r'\d+\.\d+\.\d+')
            self.assertIn('text/plain', hdrs.get('Content-Type', ''))

    def test_hosts_returns_single_mock_host(self):
        # bootstrap-config.md §5: the UI node server takes res.data[0] from /hosts
        # for heavy commands; a one-element list keeps everything on the mock.
        for port in self.each():
            status, body, _ = call(port, 'GET', '/hosts')
            self.assertEqual(status, 200)
            self.assertEqual(body, [f'localhost:{port}'])

    def test_hosts_all_is_a_list(self):
        # empirical-findings: /hosts/all feeds the System page which expects objects;
        # an empty list is the safe stub.
        for port in self.each():
            status, body, _ = call(port, 'GET', '/hosts/all')
            self.assertEqual(status, 200)
            self.assertEqual(body, [])

    def test_api_discovery_lists_versions(self):
        for port in self.each():
            status, body, _ = call(port, 'GET', '/api/')
            self.assertEqual(status, 200)
            self.assertEqual(body, ['v3', 'v4'])


class TestAuth(BackendTestCase):
    ICEBERG_BASIC = 'Basic ' + base64.b64encode(b'iceberg:iceberg').decode()

    def test_login_success_sets_cypress_cookie(self):
        # auth.md §1: login is HTTP Basic to POST /login (NOT a JSON command);
        # The mock adds an explicit SameSite browser boundary to the real shape.
        for port in self.each():
            status, body, hdrs = call(port, 'POST', '/login',
                                      headers={'Authorization': self.ICEBERG_BASIC})
            self.assertEqual(status, 200)
            self.assertFalse(body)  # empty body
            cookie = hdrs.get('Set-Cookie', '')
            self.assertIn('YTCypressCookie=', cookie)
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Lax', cookie)

    def test_login_without_authorization_returns_empty_basic_challenge(self):
        # cypress_cookie_login.cpp:50-58,224-228 — a regular request to /login is the
        # signal to present the login form: empty 401 plus a Basic challenge.
        for port in self.each():
            status, body, hdrs = call(port, 'POST', '/login')
            self.assertEqual(status, 401)
            self.assertIsNone(body)
            self.assertEqual(hdrs.get('WWW-Authenticate'), 'Basic')
            self.assertIsNone(hdrs.get('X-YT-Error'))

    def test_login_rejects_malformed_authorization_as_400(self):
        # cypress_cookie_login.cpp:94-128 — malformed headers, unsupported auth
        # methods and syntactically invalid Basic credentials are client errors.
        no_colon = 'Basic ' + base64.b64encode(b'iceberg').decode()
        cases = [
            ('Basic',
             'Malformed "Authorization" header: failed to parse authorization method'),
            ('Bearer token', 'Unsupported authorization method "Bearer"'),
            ('Basic !!!', 'Failed to decode user credentials'),
            (no_colon, 'Failed to parse user credentials'),
        ]
        for port in self.each():
            for authorization, message in cases:
                with self.subTest(authorization=authorization):
                    status, body, hdrs = call(
                        port, 'POST', '/login', headers={'Authorization': authorization})
                    self.assertEqual(status, 400)
                    self.assertEqual(body['code'], 1)
                    self.assertEqual(body['message'], message)
                    self.assertEqual(hdrs.get('X-YT-Response-Code'), '1')
                    self.assertEqual(json.loads(hdrs.get('X-YT-Error'))['message'], message)
                    self.assertIsNone(hdrs.get('WWW-Authenticate'))

    def test_login_wrong_password_is_401_generic_code(self):
        # cypress_cookie_login.cpp:83 — the real proxy masks the failure cause as a
        # generic TError (code 1) "Incorrect login or password" with HTTP 401. The
        # body is the yt-error entity, mirrored into X-YT-* headers.
        bad = 'Basic ' + base64.b64encode(b'iceberg:wrong').decode()
        for port in self.each():
            status, body, hdrs = call(port, 'POST', '/login', headers={'Authorization': bad})
            self.assertEqual(status, 401)
            self.assertEqual(body['code'], 1)
            self.assertEqual(body['message'], 'Incorrect login or password')
            self.assertEqual(hdrs.get('X-YT-Response-Code'), '1')
            self.assertEqual(json.loads(hdrs.get('X-YT-Error'))['code'], 1)
            self.assertEqual(hdrs.get('WWW-Authenticate'), 'Basic')

    def test_login_unknown_user_is_masked_401_basic_challenge(self):
        # Resolve errors for absent Cypress users follow the same masked branch as
        # invalid passwords; callers must not be able to enumerate user names.
        unknown = 'Basic ' + base64.b64encode(b'no-such-user:anything').decode()
        for port in self.each():
            status, body, hdrs = call(
                port, 'POST', '/login', headers={'Authorization': unknown})
            self.assertEqual(status, 401)
            self.assertEqual(body['code'], 1)
            self.assertEqual(body['message'], 'Incorrect login or password')
            self.assertEqual(hdrs.get('WWW-Authenticate'), 'Basic')

    def test_whoami_without_credentials_still_succeeds(self):
        # empirical-findings + auth.md §4: with authentication:"none" the UI server
        # sends NO credentials, yet /auth/whoami must return 200 with a truthy
        # csrf_token, otherwise the UI blocks with PRELOAD_ERROR.AUTHENTICATION.
        for port in self.each():
            status, body, _ = call(port, 'GET', '/auth/whoami')
            self.assertEqual(status, 200)
            self.assertEqual(set(body), {'login', 'realm', 'real_login', 'csrf_token'})
            self.assertTrue(body['csrf_token'])

    def test_cookie_auth_requires_csrf_on_non_get(self):
        # auth.md §2: CSRF is enforced ONLY for cookie-authenticated non-GET requests
        # (http_authenticator.cpp:214-232). Token/anonymous auth is exempt.
        for port in self.each():
            _, _, hdrs = call(port, 'POST', '/login',
                              headers={'Authorization': self.ICEBERG_BASIC})
            cookie = hdrs.get('Set-Cookie', '').split(';')[0]

            # A missing CSRF header is InvalidCredentials (111); malformed or
            # bad signed values have their own semantics in test_cookie_model.
            status, body, _ = call(port, 'POST', '/api/v3/exists',
                                   body={'path': '//tmp'}, headers={'Cookie': cookie})
            self.assertEqual(status, 401)
            self.assertEqual(body['code'], 111)

            # same request with the matching X-Csrf-Token -> accepted.
            # The token is derived from whoami for this user (auth.md §2).
            _, who, _ = call(port, 'GET', '/auth/whoami', headers={'Cookie': cookie})
            status, body, _ = call(port, 'POST', '/api/v3/exists',
                                   body={'path': '//tmp'},
                                   headers={'Cookie': cookie, 'X-Csrf-Token': who['csrf_token']})
            self.assertEqual(status, 200)
            self.assertIs(body, True)

            # GET with cookie and no CSRF is always fine
            status, _, _ = call(port, 'GET',
                                '/api/v3/get?path=//tmp/@type', headers={'Cookie': cookie})
            self.assertEqual(status, 200)


class TestStrictAuth(BackendTestCase):
    """Authentication-required mode used by the PostgreSQL-enabled chart."""

    def each(self):
        yield STRICT_PORT

    def test_readiness_stays_public(self):
        for port in self.each():
            status, body, _ = call(port, 'GET', '/ready')
            self.assertEqual(status, 200)
            self.assertEqual(body, {})

    def test_missing_and_invalid_credentials_are_rejected(self):
        cases = [
            {},
            {'Cookie': 'YTCypressCookie=expired'},
            {'Authorization': 'OAuth wrong-robot-token'},
        ]
        for port in self.each():
            for headers in cases:
                with self.subTest(headers=headers):
                    status, body, _ = call(port, 'GET', '/auth/whoami', headers=headers)
                    self.assertEqual(status, 401)
                    self.assertEqual(body['code'], 900)
            status, body, _ = call(
                port, 'POST', '/api/v3/exists', body={'path': '//tmp'})
            self.assertEqual(status, 401)
            self.assertEqual(body['code'], 900)

    def test_whoami_error_header_only_on_failure(self):
        # Ported from test_cypress_token_auth.py (test_whoami_invalid_token_
        # yt_error_header / test_whoami_valid_token_no_yt_error_header).
        for port in self.each():
            status, _, hdrs = call(port, 'GET', '/auth/whoami',
                                   headers={'Authorization': 'OAuth bogus'})
            self.assertEqual(status, 401)
            self.assertIn('code', json.loads(hdrs['X-YT-Error']))
            status, _, hdrs = call(port, 'GET', '/auth/whoami',
                                   headers={'Authorization': 'OAuth protocol-test-robot'})
            self.assertEqual(status, 200)
            self.assertIsNone(hdrs.get('X-YT-Error'))

    def test_robot_token_authenticates_without_csrf(self):
        headers = {'Authorization': 'OAuth protocol-test-robot'}
        for port in self.each():
            status, who, _ = call(port, 'GET', '/auth/whoami', headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(who['login'], 'iceberg')
            self.assertEqual(who['realm'], 'mock')
            status, exists, _ = call(
                port, 'POST', '/api/v3/exists',
                body={'path': '//tmp'}, headers=headers)
            self.assertEqual(status, 200)
            self.assertIs(exists, True)

    def test_published_development_credentials_are_rejected(self):
        # The non-ASCII password must fail with the same masked 401, not a 500
        # (a distinct status would fingerprint the published login names).
        credentials = (b'iceberg:iceberg', b'root:', 'iceberg:пароль'.encode())
        for port in self.each():
            for raw_credentials in credentials:
                with self.subTest(credentials=raw_credentials):
                    basic = 'Basic ' + base64.b64encode(raw_credentials).decode()
                    status, body, headers = call(
                        port, 'POST', '/login',
                        headers={'Authorization': basic})
                    self.assertEqual(status, 401)
                    self.assertEqual(body['code'], 1)
                    self.assertEqual(
                        body['message'], 'Incorrect login or password')
                    self.assertEqual(headers.get('WWW-Authenticate'), 'Basic')


class TestCypressCommands(BackendTestCase):
    def test_get_attribute_and_nested_path(self):
        # table-viewer.md: @-paths address attributes; nested steps traverse into
        # the value ({$attributes,$value} wrappers are transparent, lists accept indices).
        for port in self.each():
            _, rc, _ = call(port, 'POST', '/api/v3/get',
                            body={'path': '//home/iceberg/warehouse/trips/@row_count'})
            self.assertEqual(rc, 250)
            _, name, _ = call(port, 'POST', '/api/v3/get',
                              body={'path': '//home/iceberg/warehouse/trips/@schema/0/name'})
            self.assertEqual(name, 'trip_id')

    def test_get_missing_attribute_is_400_code_500(self):
        # table-viewer.md error model: generic command failures are HTTP 400; the
        # YT code 500 (= resolve error) is what the UI tolerates per-attribute.
        for port in self.each():
            status, body, _ = call(port, 'POST', '/api/v3/get',
                                   body={'path': '//tmp/@no_such_attr'})
            self.assertEqual(status, 400)
            self.assertEqual(body['code'], 500)

    def test_virtual_attributes_exist_on_every_node(self):
        # empirical-findings §7 (found by playing): the Attributes tabs read
        # @opaque_attribute_keys and @user_attributes on any node; 400s here
        # surface as visible error toasts.
        for port in self.each():
            _, opaque, _ = call(port, 'POST', '/api/v3/get',
                                body={'path': '//tmp/@opaque_attribute_keys'})
            self.assertEqual(opaque, [])
            _, ua, _ = call(port, 'POST', '/api/v3/get',
                            body={'path': '//tmp/@user_attributes'})
            self.assertEqual(ua, {})

    def test_list_with_attributes_uses_dollar_wrapping(self):
        # table-viewer.md: `list` with an attributes param returns
        # {$attributes: {...}, $value: <child name>} per child (annotated JSON).
        for port in self.each():
            _, body, _ = call(port, 'POST', '/api/v3/list',
                              body={'path': '//home/iceberg/warehouse',
                                    'attributes': ['type', 'row_count']})
            names = {e['$value'] for e in body}
            self.assertEqual(names, {'trips', 'events'})
            trips = next(e for e in body if e['$value'] == 'trips')
            self.assertEqual(trips['$attributes']['type'], 'table')
            self.assertEqual(trips['$attributes']['row_count'], 250)

    def test_v4_wraps_result_in_value_envelope(self):
        # table-viewer.md §3: v4 get/list/exists wrap the result as {"value": ...};
        # v3 returns it bare. (Observed live via @has_row_level_ace -> {"value":false}.)
        for port in self.each():
            _, v3, _ = call(port, 'POST', '/api/v3/exists', body={'path': '//tmp'})
            self.assertIs(v3, True)
            _, v4, _ = call(port, 'POST', '/api/v4/exists', body={'path': '//tmp'})
            self.assertEqual(v4, {'value': True})

    def test_key_columns_present_on_tables(self):
        # empirical-findings §3: @key_columns must exist on every table (list of
        # sorted column names, [] for unsorted) or prepareColumns crashes the viewer.
        for port in self.each():
            _, kc, _ = call(port, 'POST', '/api/v3/get',
                            body={'path': '//home/iceberg/warehouse/trips/@key_columns'})
            self.assertEqual(kc, ['trip_id'])
            _, kc2, _ = call(port, 'POST', '/api/v3/get',
                             body={'path': '//home/iceberg/warehouse/events/@key_columns'})
            self.assertEqual(kc2, [])

    def test_paths_are_double_slash_absolute(self):
        # empirical-findings §2: @path must start with '//' or the UI's ypath
        # parser throws "invalid relative ypath".
        for port in self.each():
            _, p, _ = call(port, 'POST', '/api/v3/get',
                           body={'path': '//home/iceberg/warehouse/trips/@path'})
            self.assertEqual(p, '//home/iceberg/warehouse/trips')


class TestTypedOutputFormat(BackendTestCase):
    TYPED = {'$value': 'json', '$attributes': {'annotate_with_types': True, 'stringify': True}}

    def test_scalars_become_type_value_pairs(self):
        # empirical-findings §1 (the "type unknown" bug): with annotate_with_types
        # every scalar in the result must be {"$type": ..., "$value": "<string>"} —
        # numbers stringified, booleans "true"/"false". Without this the UI's
        # prepareAttributes yields type undefined and refuses to render the node.
        for port in self.each():
            _, body, _ = call(port, 'POST', '/api/v3/execute_batch',
                              body={'requests': [{'command': 'get',
                                                  'parameters': {'path': '//home/iceberg/warehouse/trips/@'}}],
                                    'output_format': self.TYPED})
            attrs = body[0]['output']
            self.assertEqual(attrs['type'], {'$type': 'string', '$value': 'table'})
            self.assertEqual(attrs['row_count'], {'$type': 'int64', '$value': '250'})
            self.assertEqual(attrs['dynamic'], {'$type': 'boolean', '$value': 'false'})
            # {$attributes,$value} wrappers survive, both halves annotated:
            self.assertIn('$attributes', attrs['schema'])
            self.assertEqual(attrs['schema']['$attributes']['strict'],
                             {'$type': 'boolean', '$value': 'true'})

    def test_without_typed_format_scalars_stay_plain(self):
        # The same request without output_format returns plain JSON scalars.
        for port in self.each():
            _, body, _ = call(port, 'POST', '/api/v3/get',
                              body={'path': '//home/iceberg/warehouse/trips/@row_count'})
            self.assertEqual(body, 250)


class TestExecuteBatch(BackendTestCase):
    def test_results_in_request_order_with_per_item_errors(self):
        # coverage-notes.md: batch failures ride inside an HTTP-200 response as
        # per-item {error}; successes as {output}. Order matches the request list.
        for port in self.each():
            status, body, _ = call(port, 'POST', '/api/v3/execute_batch',
                                   body={'requests': [
                                       {'command': 'exists', 'parameters': {'path': '//tmp'}},
                                       {'command': 'get', 'parameters': {'path': '//absent'}},
                                       {'command': 'frobnicate', 'parameters': {}}]})
            self.assertEqual(status, 200)
            self.assertEqual(body[0], {'output': True})
            self.assertEqual(body[1]['error']['code'], 500)
            self.assertIn('not registered', body[2]['error']['message'])


class TestReadTableWebJson(BackendTestCase):
    def read(self, port, path, attrs=None):
        of = {'$value': 'web_json', '$attributes': attrs or {}}
        _, body, _ = call(port, 'POST', '/api/v3/read_table',
                          body={'path': path, 'output_format': of})
        return body

    def test_range_suffix_selects_rows(self):
        # table-viewer.md: the viewer requests `path[#start:#end]` (a YPath string
        # suffix, not a ranges attribute) — 51 rows for a 50-row page.
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/trips[#10:#13]')
            self.assertEqual(len(body['rows']), 3)
            # trip_id is 1-based; row #10 -> id 11. Cells are {$type,$value:string}.
            self.assertEqual(body['rows'][0]['trip_id'], {'$type': 'int64', '$value': '11'})

    def test_annotated_ypath_range_selects_rows(self):
        # table/table.js sends this annotated-YPath form when an unmounted,
        # unsorted dynamic table falls back to the static-table reader.
        path = {
            '$value': '//home/iceberg/warehouse/trips',
            '$attributes': {
                'ranges': [{
                    'lower_limit': {'tablet_index': 0, 'row_index': 10},
                    'upper_limit': {'tablet_index': 0, 'row_index': 13},
                }],
            },
        }
        for port in self.each():
            body = self.read(port, path)
            self.assertEqual(len(body['rows']), 3)
            self.assertEqual(body['rows'][0]['trip_id'], {'$type': 'int64', '$value': '11'})

    def test_column_discovery_preload(self):
        # table-viewer.md §5: the first read_table per table-open is
        # `column_names: []` + range [#0:#0] — zero row payload, but
        # all_column_names fully populated (and sorted ascending).
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/trips[#0:#0]',
                             {'column_names': []})
            self.assertEqual(body['rows'], [])
            self.assertEqual(body['all_column_names'],
                             sorted(['trip_id', 'city', 'distance_km', 'started_at', 'is_completed']))

    def test_column_names_fully_replaces_column_count_limit(self):
        # web_json_writer.cpp semantics (table-viewer.md §5.1): when column_names
        # is present the max_selected_column_count limit is NOT applied.
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/trips[#0:#1]',
                             {'column_names': ['city', 'trip_id'], 'max_selected_column_count': 1})
            self.assertEqual(set(body['rows'][0]), {'city', 'trip_id'})

    def test_incomplete_flags_are_strings(self):
        # table-viewer.md §5.2: incomplete_columns / incomplete_all_column_names are
        # the STRINGS "true"/"false" on the wire, not JSON booleans.
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/trips[#0:#1]',
                             {'max_selected_column_count': 2})
            self.assertEqual(body['incomplete_columns'], 'true')
            self.assertIsInstance(body['incomplete_all_column_names'], str)
            self.assertEqual(len(body['rows'][0]), 2)

    def test_any_column_is_json_stringified(self):
        # table-viewer.md §5.3 (schemaless value_format): composite values are
        # emitted as {"$type":"any","$value":"<compact JSON string>"}.
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/events[#0:#1]')
            cell = body['rows'][0]['payload']
            self.assertEqual(cell['$type'], 'any')
            self.assertEqual(json.loads(cell['$value']),
                             {'kind': 'click', 'page': '/p/0', 'coords': [0, 0]})

    def test_double_stringification_matches_js(self):
        # Consistency detail: integer-valued doubles print without a decimal point
        # ("3", not "3.0") — the encoder must match the JavaScript-facing wire contract.
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/trips[#0:#1]')
            self.assertEqual(body['rows'][0]['distance_km'], {'$type': 'double', '$value': '3'})

    def test_read_table_on_non_table_is_400(self):
        for port in self.each():
            status, body, _ = call(port, 'POST', '/api/v3/read_table',
                                   body={'path': '//home/iceberg/warehouse',
                                         'output_format': 'json'})
            self.assertEqual(status, 400)
            self.assertEqual(body['code'], 500)


class TestHostsRoleFiltering(BackendTestCase):
    def test_role_filter_matches_coordinator(self):
        # Ported from test_http_proxy.py test_hosts / test_proxy_roles.py: the
        # default filter is the "data" role (coordinator.cpp:551); this mock is
        # one data-role proxy, so any other role has no members.
        for port in self.each():
            _, default, _ = call(port, 'GET', '/hosts')
            _, data, _ = call(port, 'GET', '/hosts?role=data')
            _, control, _ = call(port, 'GET', '/hosts?role=control')
            self.assertEqual(default, [f'localhost:{port}'])
            self.assertEqual(data, default)
            self.assertEqual(control, [])


class TestErrorFormatNegotiation(BackendTestCase):
    """Ported from test_http_proxy.py test_error_format / test_error_format_type /
    test_error_web_json: X-YT-Error-Format governs the error encoding, reported
    via X-YT-Error-Content-Type."""

    def fail_get(self, port, error_format=None, path='//does_not_exist', headers=None):
        headers = dict(headers or {})
        if error_format:
            # X-YT-Error-Format is a structured value encoded according to the
            # header format, exactly as in the upstream integration tests.
            headers.update({'X-YT-Header-Format': '<format=text>yson',
                            'X-YT-Error-Format': error_format})
        return call(port, 'POST', '/api/v3/get', body={'path': path},
                    headers=headers)

    def test_default_is_plain_json(self):
        for port in self.each():
            status, body, hdrs = self.fail_get(port)
            self.assertEqual(status, 400)
            self.assertEqual(hdrs['X-YT-Error-Content-Type'], 'application/json')
            self.assertIsInstance(json.loads(hdrs['X-YT-Error'])['code'], int)

    def test_annotate_with_types_json(self):
        for port in self.each():
            _, body, hdrs = self.fail_get(port, '<annotate_with_types=%true>json')
            err = json.loads(hdrs['X-YT-Error'])
            self.assertEqual(err['code'], {'$type': 'int64', '$value': '500'})
            self.assertIn('$type', err['attributes']['code'])
            self.assertIsInstance(body['code'], int)
            self.assertEqual(hdrs['Content-Type'], 'application/json')

    def test_yson_text(self):
        for port in self.each():
            _, body, hdrs = self.fail_get(port, '<format=text>yson')
            self.assertEqual(hdrs['X-YT-Error-Content-Type'], 'application/x-yt-yson-text')
            self.assertTrue(hdrs['X-YT-Error'].startswith('{"code"=500;'))
            self.assertIsInstance(body, dict)
            self.assertEqual(body['code'], 500)
            self.assertEqual(hdrs['Content-Type'], 'application/json')

    def test_yson_escapes_control_characters(self):
        for port in self.each():
            _, body, hdrs = self.fail_get(port, '<format=text>yson',
                                          path='//missing\nline')
            self.assertIn('\\n', hdrs['X-YT-Error'])
            self.assertNotIn('\n', hdrs['X-YT-Error'])
            self.assertIn('\n', body['message'])

    def test_yson_escapes_unicode_as_utf8_bytes(self):
        for port in self.each():
            _, body, hdrs = self.fail_get(port, '<format=text>yson',
                                          path='//missing/café/😀')
            self.assertIn('\\xc3\\xa9', hdrs['X-YT-Error'])
            self.assertIn('\\xf0\\x9f\\x98\\x80', hdrs['X-YT-Error'])
            self.assertIn('café/😀', body['message'])

    def test_numbered_header_parts_are_base64_decoded(self):
        encoded = base64.b64encode(b'<format=text>yson').decode()
        for port in self.each():
            _, body, hdrs = self.fail_get(port, headers={
                'X-YT-Header-Format': '<format=text>yson',
                'X-YT-Error-Format-0': encoded[:12],
                'X-YT-Error-Format-1': encoded[12:],
            })
            self.assertEqual(body['code'], 500)
            self.assertEqual(hdrs['X-YT-Error-Content-Type'],
                             'application/x-yt-yson-text')

    def test_raw_yson_requires_yson_header_format(self):
        for port in self.each():
            status, body, _ = self.fail_get(
                port, headers={'X-YT-Error-Format': '<format=text>yson'})
            self.assertEqual(status, 400)
            self.assertIn('Unable to parse X-YT-Error-Format', body['message'])

    def test_unsupported_format_is_rejected_before_execution(self):
        headers = {'X-YT-Header-Format': '<format=text>yson',
                   'X-YT-Error-Format': 'bogus'}
        for port in self.each():
            for path in ('//tmp', '//does_not_exist'):
                status, body, _ = call(
                    port, 'POST', '/api/v3/exists', body={'path': path},
                    headers=headers)
                self.assertEqual(status, 400)
                self.assertIn('Unsupported X-YT-Error-Format', body['message'])

    def test_web_json_keeps_small_ints_plain(self):
        # The real test accepts plain ints when they fit in 2^53-1.
        for port in self.each():
            _, _, hdrs = self.fail_get(port, 'web_json')
            err = json.loads(hdrs['X-YT-Error'])
            self.assertIsInstance(err['code'], int)
            self.assertLessEqual(err['code'], 2 ** 53 - 1)

    def test_error_format_is_allowed_by_cors_preflight(self):
        status, _, hdrs = call(CORS_PORT, 'OPTIONS', '/api/v3/get', headers={
            'Origin': 'https://viewer.internal',
            'Access-Control-Request-Headers': 'X-YT-Error-Format',
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            hdrs['Access-Control-Allow-Origin'], 'https://viewer.internal')
        self.assertEqual(hdrs['Access-Control-Allow-Credentials'], 'true')
        self.assertEqual(hdrs['Vary'], 'Origin')
        allowed = {part.strip().lower() for part in
                   hdrs['Access-Control-Allow-Headers'].split(',')}
        self.assertIn('x-yt-error-format', allowed)

    def test_cors_is_default_deny_and_uses_an_exact_allowlist(self):
        origins = (
            'https://attacker.internal',
            'https://viewer.internal.attacker.example',
            'null',
        )
        for port in (PORT, STRICT_PORT, CORS_PORT):
            for origin in origins:
                status, _, headers = call(
                    port, 'OPTIONS', '/api/v3/get',
                    headers={'Origin': origin})
                self.assertEqual(status, 403, (port, origin))
                self.assertIsNone(headers.get('Access-Control-Allow-Origin'))

        status, _, headers = call(
            PORT, 'GET', '/auth/whoami',
            headers={'Origin': 'https://viewer.internal'})
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get('Access-Control-Allow-Origin'))

        _, _, login_headers = call(
            PORT, 'POST', '/login',
            headers={'Authorization': TestAuth.ICEBERG_BASIC})
        cookie = login_headers['Set-Cookie'].split(';', 1)[0]
        status, _, headers = call(
            PORT, 'GET', '/auth/whoami',
            headers={
                'Cookie': cookie,
                'Origin': 'https://attacker.internal',
            })
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get('Access-Control-Allow-Origin'))


class TestReadTableYqlFormat(BackendTestCase):
    """value_format: yql (table-viewer.md §5.4, web_json_writer.cpp:110-345):
    cells are [value, "<registry index>"], present optionals wrap as [inner],
    null stays null; numbers stringified, booleans native JSON; any/Yson values
    are {"val": <$type/$value tree>}; yql_type_registry is deduplicated."""

    def read(self, port, path, attrs):
        of = {'$value': 'web_json', '$attributes': {**attrs, 'value_format': 'yql'}}
        _, body, _ = call(port, 'POST', '/api/v3/read_table',
                          body={'path': path, 'output_format': of})
        return body

    def test_registry_and_scalar_cells(self):
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/trips[#0:#2]', {})
            # trips: int64, string, double, string, boolean -> 4 distinct types,
            # the two string columns share one registry entry.
            self.assertEqual(body['yql_type_registry'], [
                ['OptionalType', ['DataType', 'Int64']],
                ['OptionalType', ['DataType', 'String']],
                ['OptionalType', ['DataType', 'Double']],
                ['OptionalType', ['DataType', 'Boolean']]])
            row = body['rows'][0]
            self.assertEqual(row['trip_id'], [['1'], '0'])
            self.assertEqual(row['city'], [['Amsterdam'], '1'])
            self.assertEqual(row['distance_km'], [['3'], '2'])       # stringified, JS-style
            self.assertEqual(row['started_at'][1], '1')              # shares String entry
            self.assertEqual(row['is_completed'], [[False], '3'])    # native JSON boolean

    def test_yson_column_wraps_typed_tree(self):
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/events[#0:#1]', {})
            self.assertEqual(body['yql_type_registry'][2], ['OptionalType', ['DataType', 'Yson']])
            cell = body['rows'][0]['payload']
            self.assertEqual(cell[1], '2')
            val = cell[0][0]['val']
            self.assertEqual(val['kind'], {'$type': 'string', '$value': 'click'})
            self.assertEqual(val['coords'][0], {'$type': 'int64', '$value': '0'})

    def test_column_projection_still_applies(self):
        for port in self.each():
            body = self.read(port, '//home/iceberg/warehouse/trips[#0:#1]',
                             {'column_names': ['is_completed']})
            self.assertEqual(list(body['rows'][0]), ['is_completed'])
            self.assertEqual(len(body['yql_type_registry']), 1)

    def test_schemaless_stays_default(self):
        # Without value_format the cells keep the $type/$value shape and no registry.
        for port in self.each():
            _, body, _ = call(port, 'POST', '/api/v3/read_table',
                              body={'path': '//home/iceberg/warehouse/trips[#0:#1]',
                                    'output_format': {'$value': 'web_json', '$attributes': {}}})
            self.assertNotIn('yql_type_registry', body)
            self.assertEqual(body['rows'][0]['trip_id'], {'$type': 'int64', '$value': '1'})


class TestEmptyStateCommands(BackendTestCase):
    """Operations/Queries pages: empty-but-valid answers instead of 404 error
    blocks (shapes per scheduler_commands.cpp:417-441, query_commands.cpp:429-437)."""

    def test_list_operations_empty_result(self):
        for port in self.each():
            status, body, _ = call(port, 'POST', '/api/v3/list_operations',
                                   body={'filter': '', 'include_counters': True})
            self.assertEqual(status, 200)
            self.assertEqual(body['operations'], [])
            self.assertIs(body['incomplete'], False)
            self.assertEqual(body['state_counts'], {})
            self.assertEqual(body['failed_jobs_count'], 0)

    def test_get_query_tracker_info_empty_capabilities(self):
        for port in self.each():
            status, body, _ = call(port, 'GET', '/api/v4/get_query_tracker_info')
            self.assertEqual(status, 200)
            self.assertEqual(body['cluster_name'], 'mock')
            self.assertEqual(body['supported_features'], {})
            self.assertEqual(body['access_control_objects'], [])

    def test_sys_users_and_groups_list_empty(self):
        # Operations page loads the user filter from //sys/users; empty beats a toast.
        for port in self.each():
            for path in ('//sys/users', '//sys/groups'):
                status, body, _ = call(port, 'POST', '/api/v3/list', body={'path': path})
                self.assertEqual(status, 200)
                self.assertEqual(body, [])


class TestErrorEnvelope(BackendTestCase):
    def test_error_body_is_yt_error_entity_with_headers(self):
        # coverage-notes.md conventions: every error response is the yt-error
        # entity, mirrored into X-YT-Error / X-YT-Response-Code / X-YT-Response-Message.
        for port in self.each():
            status, body, hdrs = call(port, 'POST', '/api/v3/get', body={'path': '//nope'})
            self.assertEqual(status, 400)
            self.assertEqual(set(body), {'code', 'message', 'attributes', 'inner_errors'})
            self.assertEqual(json.loads(hdrs['X-YT-Error']), body)
            self.assertEqual(hdrs['X-YT-Response-Code'], str(body['code']))
            self.assertEqual(hdrs['X-YT-Response-Message'], body['message'])

    def test_unknown_command_is_404(self):
        for port in self.each():
            status, body, _ = call(port, 'POST', '/api/v3/frobnicate', body={})
            self.assertEqual(status, 404)
            self.assertIn('not registered', body['message'])


class TestConnectionManagement(BackendTestCase):
    def test_connection_decision_is_advertised(self):
        # Hard-won empirical rule (docs/empirical-findings.md): an HTTP/1.1
        # response with NO Connection header implies keep-alive to Node clients.
        # A server that then closes the socket (as Python's http.server did
        # silently for `Connection: close` requests) leaves clients pooling
        # dead sockets -> intermittent "socket hang up" -> 504s in the UI.
        # The invariant: a header-less response means the socket MUST actually
        # stay usable, and a close decision must be advertised.
        # urllib always injects its own `Connection: close`, so use raw http.client.
        import http.client
        for port in self.each():
            conn = http.client.HTTPConnection('localhost', port, timeout=5)
            conn.request('GET', '/ping', headers={'Connection': 'keep-alive'})
            r = conn.getresponse()
            r.read()
            self.assertIn(r.getheader('Connection'), (None, 'keep-alive'))
            self.assertFalse(r.will_close)
            # the same socket must be reusable — the promise the header implies
            conn.request('GET', '/ping', headers={'Connection': 'keep-alive'})
            r2 = conn.getresponse()
            r2.read()
            self.assertEqual(r2.status, 200)
            conn.close()

            conn = http.client.HTTPConnection('localhost', port, timeout=5)
            conn.request('GET', '/ping', headers={'Connection': 'close'})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.getheader('Connection'), 'close')
            conn.close()

    def test_burst_of_parallel_connections(self):
        # The UI node server opens ~20 parallel connections per page load; the
        # listen backlog must absorb the burst (http.server default is 5 -> resets).
        import threading
        for port in self.each():
            errors = []

            def one():
                try:
                    status, body, _ = call(port, 'POST', '/api/v3/exists', body={'path': '//tmp'})
                    if status != 200 or body is not True:
                        errors.append((status, body))
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))

            threads = [threading.Thread(target=one) for _ in range(30)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])


class TestParameterSources(BackendTestCase):
    def test_query_string_header_and_body_all_work(self):
        # coverage-notes.md parameter-decoding: precedence is query string <-
        # X-YT-Parameters <- JSON body; each source alone must work.
        for port in self.each():
            _, q, _ = call(port, 'GET',
                           '/api/v3/get?path=//home/iceberg/warehouse/trips/@row_count')
            self.assertEqual(q, 250)
            _, h, _ = call(port, 'POST', '/api/v3/get', headers={
                'X-YT-Parameters': '{"path":"//home/iceberg/warehouse/trips/@type"}'})
            self.assertEqual(h, 'table')
            _, b, _ = call(port, 'POST', '/api/v3/get',
                           body={'path': '//home/iceberg/warehouse/trips/@dynamic'})
            self.assertIs(b, False)

    def test_audit_log_table_exposes_strict_columns_only(self):
        # mock-backend-py/README.md "Audit log": the trail is browsable at
        # //sys/logs/audit_log with ts/login/endpoint only — the schemaless
        # `details` payload is never selected, so it cannot leak here.
        for port in self.each():
            call(port, 'POST', '/api/v3/get',
                 body={'path': '//home/iceberg/warehouse/trips/@type'})
            _, count, _ = call(port, 'POST', '/api/v3/get',
                               body={'path': '//sys/logs/audit_log/@row_count'})
            _, body, _ = call(port, 'POST', '/api/v3/read_table', body={
                'path': f'//sys/logs/audit_log[#{max(0, count - 50)}:#{count}]',
                'output_format': {'$value': 'web_json',
                                  '$attributes': {'max_selected_column_count': 50}}})
            self.assertEqual(body['all_column_names'],
                             ['endpoint', 'http_code', 'login', 'ts'])
            self.assertGreater(len(body['rows']), 0)
            # Other suites share this backend, so search instead of relying
            # on row order.
            row = next(r for r in reversed(body['rows'])
                       if r['endpoint']['$value'] == '/api/v3/get')
            self.assertEqual(set(row), {'ts', 'login', 'endpoint', 'http_code'})
            self.assertEqual(row['login']['$value'], 'iceberg')
            self.assertEqual(row['http_code'], {'$type': 'int64', '$value': '200'})
            self.assertRegex(row['ts']['$value'], r'^\d{4}-\d{2}-\d{2}T.*Z$')

            _, count_before, _ = call(port, 'POST', '/api/v3/get',
                                      body={'path': '//sys/logs/audit_log/@row_count'})
            call(port, 'POST', '/api/v3/exists', body={'path': '//tmp'})
            _, count_after, _ = call(port, 'POST', '/api/v3/get',
                                     body={'path': '//sys/logs/audit_log/@row_count'})
            self.assertGreater(count_after, count_before)  # the table is live

    def test_system_page_paths_resolve_empty(self):
        # store/actions/system/{nodes.ts,schedulers.js,rpc-proxies.ts,
        # resources.js}: the System page throws error blocks on ANY failed
        # batch item; every path it reads must resolve (empty is fine).
        for port in self.each():
            _, batch, _ = call(port, 'POST', '/api/v3/execute_batch', body={'requests': [
                {'command': 'list', 'parameters': {'path': '//sys/racks'}},
                *({'command': 'list',
                   'parameters': {'path': f'//sys/{t}',
                                  'attributes': ['state', 'banned', 'rack']}}
                  for t in ('cluster_nodes', 'data_nodes', 'exec_nodes',
                            'tablet_nodes', 'chaos_nodes')),
                {'command': 'list', 'parameters': {'path': '//sys/scheduler/instances'}},
                {'command': 'list',
                 'parameters': {'path': '//sys/controller_agents/instances'}},
                {'command': 'get', 'parameters': {'path': '//sys/rpc_proxies'}},
                {'command': 'get', 'parameters': {'path': '//sys/scheduler/@alerts'}},
                {'command': 'get',
                 'parameters': {'path': '//sys/scheduler/orchid/scheduler/cluster'}},
                {'command': 'get', 'parameters': {'path': '//sys/cluster_nodes/@'}}]})
            for item in batch:
                self.assertIn('output', item, item)
            self.assertEqual(batch[-4]['output'], {})   # rpc_proxies: no proxies
            self.assertEqual(batch[-3]['output'], [])   # scheduler alerts
            self.assertIn('resource_limits', batch[-2]['output'])
            # Resources.js prepareDiskResources indexes these without guards.
            attrs = batch[-1]['output']
            self.assertEqual(attrs['available_space_per_medium'], {'default': 0})
            self.assertEqual(attrs['used_space_per_medium'], {'default': 0})

            # masters.ts throws unless primary AND secondary resolve, and
            # hard-requires the primary master's cell_id orchid.
            _, batch, _ = call(port, 'POST', '/api/v3/execute_batch', body={'requests': [
                {'command': 'get', 'parameters': {'path': '//sys/secondary_masters'}},
                {'command': 'get', 'parameters': {'path': '//sys/timestamp_providers'}},
                {'command': 'get', 'parameters': {'path': '//sys/primary_masters/'
                    'primary-master/orchid/config/primary_master/cell_id'}}]})
            for item in batch:
                self.assertIn('output', item, item)
            self.assertRegex(batch[-1]['output'], r'^[0-9a-f-]+$')

            # chunks.js crashes on `chunks.chunks.count` if the virtual chunk
            # maps are missing; flags feed the replication indicators.
            _, chunks, _ = call(port, 'POST', '/api/v3/get',
                                body={'path': '//sys/chunks/@'})
            self.assertEqual((chunks['count'], chunks['multicell_count']), (0, {}))
            _, flags, _ = call(port, 'POST', '/api/v3/get',
                               body={'path': '//sys/@chunk_replicator_enabled'})
            self.assertIs(flags, True)

    def test_cluster_params_version_paths_resolve(self):
        # cluster-params.ts:88,149,173: the UI server's boot batches read the
        # scheduler and first-primary-master orchid versions. ui >= 1.60
        # crashes on an undefined version (support.ts .match), so these must
        # be real "N.N.N-..." strings, not batch errors.
        for port in self.each():
            _, masters, _ = call(port, 'POST', '/api/v3/list',
                                 body={'path': '//sys/primary_masters'})
            self.assertEqual(masters, ['primary-master'])
            _, batch, _ = call(port, 'POST', '/api/v3/execute_batch', body={'requests': [
                {'command': 'get',
                 'parameters': {'path': '//sys/scheduler/orchid/service/version'}},
                {'command': 'get',
                 'parameters': {'path': f'//sys/primary_masters/{masters[0]}'
                                        '/orchid/service/version'}}]})
            for item in batch:
                self.assertIn('output', item)
                self.assertRegex(item['output'], r'^\d+\.\d+\.\d+')

    def test_base64_numbered_parameter_headers(self):
        # ytsaurus-ui 1.60.x sends parameters as base64 numbered header parts
        # (X-YT-Parameters-0..N) like the real proxy's gathered YT headers.
        # Regression: the compose UI's getPoolDefaultPoolTreeName request.
        encoded = base64.b64encode(json.dumps({
            'path': '//sys/pool_trees/@default_tree',
            'suppress_access_tracking': 'true'}).encode()).decode()
        for port in self.each():
            status, tree, _ = call(port, 'GET', '/api/v3/get', headers={
                'X-YT-Parameters-0': encoded,
                'X-YT-Header-Format': '<encode_utf8=%false>json'})
            self.assertEqual(status, 200)
            self.assertEqual(tree, 'physical')

            split_at = len(encoded) // 2  # payloads may span several parts
            status, tree, _ = call(port, 'GET', '/api/v3/get', headers={
                'X-YT-Parameters-0': encoded[:split_at],
                'X-YT-Parameters-1': encoded[split_at:]})
            self.assertEqual(status, 200)
            self.assertEqual(tree, 'physical')

            status, body, _ = call(port, 'GET', '/api/v3/get', headers={
                'X-YT-Parameters-0': 'not*base64'})
            self.assertEqual(status, 400)
            self.assertIn('Unable to parse X-YT-Parameters header', body['message'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
