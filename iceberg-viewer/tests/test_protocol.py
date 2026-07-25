#!/usr/bin/env python3
"""Protocol conformance tests for the mock YT backends (Node and Python).

Every test asserts *documented* wire behavior — each carries a reference to the
doc that specifies it (docs/auth.md, docs/table-viewer.md, docs/bootstrap-config.md,
docs/empirical-findings.md). The suite runs each assertion against BOTH backends,
so it is simultaneously:
  1. a conformance check against the documented protocol, and
  2. a Node <-> Python consistency check (complements recordings/replay-diff.py,
     which replays the recorded UI traffic verbatim).

Run:  python3 tests/test_protocol.py          (spins up both servers itself)
      BACKEND=node|python python3 tests/...   (restrict to one backend)
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
PORTS = {'node': 8011, 'python': 8012}
_only = os.environ.get('BACKEND')
BACKENDS = {k: v for k, v in PORTS.items() if not _only or k == _only}

_procs = []


def setUpModule():
    if 'node' in BACKENDS:
        _procs.append(subprocess.Popen(
            ['node', str(ROOT / 'mock-backend' / 'server.js'), str(PORTS['node'])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if 'python' in BACKENDS:
        _procs.append(subprocess.Popen(
            [sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(PORTS['python'])],
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


class Both(unittest.TestCase):
    """Base: runs each check against every backend via subTest."""

    def each(self):
        for name, port in BACKENDS.items():
            with self.subTest(backend=name):
                yield port


class TestInfrastructure(Both):
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


class TestAuth(Both):
    ICEBERG_BASIC = 'Basic ' + base64.b64encode(b'iceberg:iceberg').decode()

    def test_login_success_sets_cypress_cookie(self):
        # auth.md §1: login is HTTP Basic to POST /login (NOT a JSON command);
        # success = empty 200 + Set-Cookie: YTCypressCookie=...; HttpOnly; no SameSite.
        for port in self.each():
            status, body, hdrs = call(port, 'POST', '/login',
                                      headers={'Authorization': self.ICEBERG_BASIC})
            self.assertEqual(status, 200)
            self.assertFalse(body)  # empty body
            cookie = hdrs.get('Set-Cookie', '')
            self.assertIn('YTCypressCookie=', cookie)
            self.assertIn('HttpOnly', cookie)
            self.assertNotIn('SameSite', cookie)  # real proxy never sets it

    def test_login_wrong_password_is_401_code_900(self):
        # auth.md §5: the proxy maps auth failures to HTTP 401 with YT code 900;
        # the body is the yt-error entity, mirrored into X-YT-* headers.
        bad = 'Basic ' + base64.b64encode(b'iceberg:wrong').decode()
        for port in self.each():
            status, body, hdrs = call(port, 'POST', '/login', headers={'Authorization': bad})
            self.assertEqual(status, 401)
            self.assertEqual(body['code'], 900)
            self.assertEqual(hdrs.get('X-YT-Response-Code'), '900')
            self.assertEqual(json.loads(hdrs.get('X-YT-Error'))['code'], 900)

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

            # cookie without CSRF token -> 401 code 901
            status, body, _ = call(port, 'POST', '/api/v3/exists',
                                   body={'path': '//tmp'}, headers={'Cookie': cookie})
            self.assertEqual(status, 401)
            self.assertEqual(body['code'], 901)

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


class TestCypressCommands(Both):
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


class TestTypedOutputFormat(Both):
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


class TestExecuteBatch(Both):
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


class TestReadTableWebJson(Both):
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
        # ("3", not "3.0") — the Python port must match the Node backend byte-for-byte.
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


class TestErrorEnvelope(Both):
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


class TestConnectionManagement(Both):
    def test_connection_decision_is_advertised(self):
        # Hard-won empirical rule (see mock-backend-py/server.py send_body): an
        # HTTP/1.1 response with NO Connection header implies keep-alive to Node
        # clients. A server that then closes the socket (as Python's http.server
        # does silently for `Connection: close` requests) leaves clients pooling
        # dead sockets -> intermittent "socket hang up" -> 504s in the UI.
        # Both backends must therefore say what they'll do:
        #   request Connection: keep-alive -> response Connection: keep-alive + Keep-Alive
        #   request Connection: close      -> response Connection: close
        # urllib always injects its own `Connection: close`, so use raw http.client.
        import http.client
        for port in self.each():
            conn = http.client.HTTPConnection('localhost', port, timeout=5)
            conn.request('GET', '/ping', headers={'Connection': 'keep-alive'})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.getheader('Connection'), 'keep-alive')
            self.assertIn('timeout', r.getheader('Keep-Alive') or '')
            # the same socket must be reusable
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


class TestParameterSources(Both):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
