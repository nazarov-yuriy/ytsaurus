#!/usr/bin/env python3
"""Slow-catalog simulation tests (MOCK_DELAY) against the Python backend.

Verifies the contract documented in docs/timeouts.md: data commands are delayed,
//sys paths and infrastructure endpoints never are (the UI server's boot-path
robot requests only have a 5s timeout), batches delay per sub-command, and
delayed responses are still byte-correct.

Run: python3 tests/test_slow_backend.py   (~15s: real sleeps are exercised)
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8032
AUDIT_PORT = 8033
DELAY_SPEC = 'list:1200,read_table:2000,get:800'

_procs = []


def setUpModule():
    env = {**os.environ, 'MOCK_DELAY': DELAY_SPEC}
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


def timed_call(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f'http://localhost:{port}{path}', data=data,
                                 headers={'Content-Type': 'application/json'} if data else {},
                                 method=method)
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    return status, json.loads(raw) if raw else None, time.monotonic() - t0


class TestSlowBackend(unittest.TestCase):
    def each(self):
        yield PORT

    def test_data_list_is_delayed_and_correct(self):
        for port in self.each():
            status, body, took = timed_call(port, 'POST', '/api/v3/list',
                                            body={'path': '//home/iceberg/warehouse'})
            self.assertEqual(status, 200)
            self.assertEqual(sorted(body), ['events', 'trips'])
            self.assertGreaterEqual(took, 1.2)

    def test_read_table_is_delayed_and_correct(self):
        for port in self.each():
            status, body, took = timed_call(
                port, 'POST', '/api/v3/read_table',
                body={'path': '//home/iceberg/warehouse/trips[#0:#2]',
                      'output_format': {'$value': 'web_json', '$attributes': {}}})
            self.assertEqual(status, 200)
            self.assertEqual(len(body['rows']), 2)
            self.assertGreaterEqual(took, 2.0)

    def test_sys_paths_are_never_delayed(self):
        # cluster-params robot requests (//sys/media etc.) run with a 5s timeout;
        # the delay must not apply there or the UI would fail to boot.
        for port in self.each():
            status, _, took = timed_call(port, 'POST', '/api/v3/list', body={'path': '//sys/media'})
            self.assertEqual(status, 200)
            self.assertLess(took, 0.7)
            status, _, took = timed_call(port, 'POST', '/api/v3/get',
                                         body={'path': '//sys/pool_trees/@default_tree'})
            self.assertEqual(status, 200)
            self.assertLess(took, 0.7)

    def test_infrastructure_endpoints_are_never_delayed(self):
        for port in self.each():
            for path in ('/ping', '/version', '/auth/whoami', '/ready'):
                t0 = time.monotonic()
                urllib.request.urlopen(f'http://localhost:{port}{path}', timeout=5)
                self.assertLess(time.monotonic() - t0, 0.7, path)

    def test_batch_delays_apply_per_subcommand(self):
        # A user-path get inside execute_batch is delayed; a //sys one is not.
        for port in self.each():
            status, body, took = timed_call(
                port, 'POST', '/api/v3/execute_batch',
                body={'requests': [
                    {'command': 'get', 'parameters': {'path': '//home/iceberg/warehouse/trips/@type'}},
                    {'command': 'get', 'parameters': {'path': '//sys/pool_trees/@default_tree'}}]})
            self.assertEqual(status, 200)
            self.assertEqual(body[0]['output'], 'table')
            self.assertGreaterEqual(took, 0.8)
            self.assertLess(took, 1.6)  # one delayed sub-command, not two

    def test_slow_request_does_not_block_fast_ones(self):
        # The async transport keeps /ping independent from a blocking command.
        for port in self.each():
            slow = threading.Thread(target=timed_call, args=(port, 'POST', '/api/v3/read_table'),
                                    kwargs={'body': {'path': '//home/iceberg/warehouse/trips[#0:#1]',
                                                     'output_format': 'json'}})
            slow.start()
            time.sleep(0.3)  # slow request is now in flight
            t0 = time.monotonic()
            urllib.request.urlopen(f'http://localhost:{port}/ping', timeout=5)
            self.assertLess(time.monotonic() - t0, 0.7)
            slow.join()

    def test_ping_bypasses_saturated_handler_pool(self):
        # AnyIO normally grants sync FastAPI handlers 40 worker tokens. More
        # simultaneous delayed commands than that must not queue the probe.
        count = 48
        barrier = threading.Barrier(count + 1)

        def delayed_list():
            barrier.wait(timeout=5)
            return timed_call(
                PORT, 'POST', '/api/v3/list',
                body={'path': '//home/iceberg/warehouse'})

        with ThreadPoolExecutor(max_workers=count) as clients:
            calls = [clients.submit(delayed_list) for _ in range(count)]
            barrier.wait(timeout=5)
            time.sleep(0.3)  # the first 40 handlers are sleeping in MOCK_DELAY
            t0 = time.monotonic()
            urllib.request.urlopen(f'http://localhost:{PORT}/ping', timeout=5)
            self.assertLess(time.monotonic() - t0, 0.7)
            self.assertTrue(all(call.result()[0] == 200 for call in calls))

    def test_stalled_audit_does_not_block_ping(self):
        # Run a second server whose audit store signals entry, then blocks. The
        # API response must still wait for its audit, while /ping stays live.
        wrapper = """
import os
import time
from pathlib import Path
import server

def stalled_audit(*_args):
    Path(os.environ['MOCK_AUDIT_TEST_MARKER']).touch()
    time.sleep(1.5)

server.userdb.audit = stalled_audit
server._AUDIT_TIMEOUT_SECONDS = 0.5
server.uvicorn.run(
    server.app, host='', port=server.PORT, log_level='warning',
    timeout_keep_alive=5)
"""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'audit-entered'
            env = {key: value for key, value in os.environ.items()
                   if key not in ('MOCK_DELAY', 'MOCK_PG_DSN', 'MOCK_RECORD')}
            env['MOCK_AUDIT_TEST_MARKER'] = str(marker)
            env['PYTHONPATH'] = os.pathsep.join(filter(None, (
                str(ROOT / 'mock-backend-py'), env.get('PYTHONPATH', ''))))
            process = subprocess.Popen(
                [sys.executable, '-c', wrapper, str(AUDIT_PORT)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    try:
                        urllib.request.urlopen(
                            f'http://localhost:{AUDIT_PORT}/ping', timeout=1)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail('audit-stall backend did not start')

                result = []
                audited = threading.Thread(
                    target=lambda: result.append(timed_call(
                        AUDIT_PORT, 'POST', '/api/v3/get',
                        body={'path': '//home'})))
                audited.start()
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), 'audit function was not entered')
                self.assertTrue(audited.is_alive(), 'response escaped before its audit')

                t0 = time.monotonic()
                urllib.request.urlopen(
                    f'http://localhost:{AUDIT_PORT}/ping', timeout=5)
                self.assertLess(time.monotonic() - t0, 0.7)
                audited.join(timeout=5)
                self.assertFalse(audited.is_alive())
                self.assertEqual(result[0][0], 200)
                self.assertGreaterEqual(result[0][2], 0.45)
                self.assertLess(result[0][2], 1.2)
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
