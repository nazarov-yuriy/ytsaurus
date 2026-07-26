#!/usr/bin/env python3
"""Slow-catalog simulation tests (MOCK_DELAY) against both backends.

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
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTS = {'node': 8031, 'python': 8032}
DELAY_SPEC = 'list:1200,read_table:2000,get:800'
_only = os.environ.get('BACKEND')
BACKENDS = {k: v for k, v in PORTS.items() if not _only or k == _only}

_procs = []


def setUpModule():
    env = {**os.environ, 'MOCK_DELAY': DELAY_SPEC}
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
        for name, port in BACKENDS.items():
            with self.subTest(backend=name):
                yield port

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
        # Node is single-threaded: the delay must be async. Python is
        # thread-per-connection. Either way /ping stays fast during a slow read.
        import threading
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
