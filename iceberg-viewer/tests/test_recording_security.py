#!/usr/bin/env python3
"""Security checks for the development-only MOCK_RECORD traffic fixture."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'mock-backend-py'
REDACTED = '<redacted>'
SERVER_WRAPPER = """
import server
server.RECORD_FILE_LIMIT_BYTES = int(
    __import__('os').environ.get('TEST_RECORD_LIMIT') or
    server.RECORD_FILE_LIMIT_BYTES)
server.uvicorn.run(
    server.app, host='', port=server.PORT, log_level='warning',
    timeout_keep_alive=5)
"""


def free_port():
    with socket.socket() as probe:
        probe.bind(('localhost', 0))
        return probe.getsockname()[1]


def clean_environment():
    excluded = {
        'MOCK_DELAY',
        'MOCK_ENABLE_DEV_SEED_USERS',
        'MOCK_PG_DSN',
        'MOCK_RECORD',
        'MOCK_REQUIRE_AUTH',
        'MOCK_ROBOT_TOKEN',
        'MOCK_YT_UPSTREAM',
    }
    return {key: value for key, value in os.environ.items()
            if key not in excluded}


def start_backend(record_path, **extra_environment):
    port = free_port()
    environment = {
        **clean_environment(),
        'MOCK_RECORD': str(record_path),
        'PYTHONPATH': os.pathsep.join(filter(None, (
            str(BACKEND), os.environ.get('PYTHONPATH', '')))),
        **extra_environment,
    }
    process = subprocess.Popen(
        [sys.executable, '-c', SERVER_WRAPPER, str(port)],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError('recording backend exited during startup')
        try:
            urllib.request.urlopen(
                f'http://localhost:{port}/ping', timeout=1).close()
            return process, port
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError('recording backend did not start')


def call(port, method, path, body=None, headers=None):
    data = body
    request_headers = dict(headers or {})
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        request_headers.setdefault('Content-Type', 'application/json')
    elif isinstance(body, str):
        data = body.encode()
    request = urllib.request.Request(
        f'http://localhost:{port}{path}', data=data,
        headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class TestRecordingSecurity(unittest.TestCase):
    def test_credentials_are_redacted_from_every_recording_surface(self):
        secrets = (
            'header-authorization-secret',
            'header-cookie-secret',
            'header-csrf-secret',
            'opaque-parameter-secret',
            'query-access-secret',
            'query-code-secret',
            'query-state-secret',
            'request-password-secret',
            'request-client-secret',
            'request-refresh-secret',
            'nested-bearer-secret',
        )
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / 'traffic.jsonl'
            process, port = start_backend(recording)
            try:
                status, _ = call(
                    port, 'POST',
                    '/api/v3/get?access_token=query-access-secret'
                    '&code=query-code-secret&state=query-state-secret&safe=visible',
                    body={
                        'path': '//home',
                        'password': 'request-password-secret',
                        'nested': {
                            'clientSecret': 'request-client-secret',
                            'refresh_token': 'request-refresh-secret',
                            'note': 'Bearer nested-bearer-secret',
                            'token_count': 3,
                        },
                    },
                    headers={
                        'Authorization': 'OAuth header-authorization-secret',
                        'Cookie': 'session=header-cookie-secret',
                        'X-Csrf-Token': 'header-csrf-secret',
                        'X-YT-Parameters': 'opaque-parameter-secret',
                    })
                self.assertEqual(status, 200)
                self.assertEqual(call(
                    port, 'GET', '/auth/whoami',
                    headers={'Authorization':
                             'OAuth header-authorization-secret'})[0], 200)
                self.assertEqual(call(
                    port, 'POST', '/ping', body='unstructured-body-secret',
                    headers={'Content-Type': 'text/plain'})[0], 200)
            finally:
                process.terminate()
                process.wait(timeout=10)

            raw = recording.read_text()
            for secret in (*secrets, 'unstructured-body-secret'):
                self.assertNotIn(secret, raw)
            entries = [json.loads(line) for line in raw.splitlines()]
            command = next(
                entry for entry in entries
                if entry['path'] == '/api/v3/get')
            self.assertEqual(command['request_headers']['authorization'], REDACTED)
            self.assertEqual(command['request_headers']['cookie'], REDACTED)
            self.assertEqual(command['request_headers']['x-csrf-token'], REDACTED)
            self.assertEqual(command['request_headers']['x-yt-parameters'], REDACTED)
            query = dict(urllib.parse.parse_qsl(command['query'].removeprefix('?')))
            self.assertEqual(query, {
                'access_token': REDACTED,
                'code': REDACTED,
                'state': REDACTED,
                'safe': 'visible',
            })
            self.assertEqual(command['request_body']['path'], '//home')
            self.assertEqual(command['request_body']['password'], REDACTED)
            self.assertEqual(
                command['request_body']['nested']['clientSecret'], REDACTED)
            self.assertEqual(
                command['request_body']['nested']['refresh_token'], REDACTED)
            self.assertEqual(
                command['request_body']['nested']['note'], f'Bearer {REDACTED}')
            self.assertEqual(command['request_body']['nested']['token_count'], 3)

            whoami = next(
                entry for entry in entries
                if entry['path'] == '/auth/whoami')
            self.assertEqual(whoami['response_body']['csrf_token'], REDACTED)
            opaque = [
                entry for entry in entries
                if entry['path'] == '/ping'
                and entry['request_body'] is not None
            ][0]
            self.assertEqual(opaque['request_body'], {
                '_recording_omitted': 'non-JSON body',
                'byte_length': len('unstructured-body-secret'),
            })

    def test_recording_io_failure_does_not_break_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            process, port = start_backend(directory)
            try:
                self.assertEqual(call(port, 'GET', '/ping')[0], 200)
                self.assertEqual(call(port, 'GET', '/version')[0], 200)
            finally:
                process.terminate()
                process.wait(timeout=10)

    def test_recording_file_has_a_hard_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / 'bounded.jsonl'
            limit = 1800
            process, port = start_backend(
                recording, TEST_RECORD_LIMIT=str(limit))
            try:
                for _ in range(30):
                    self.assertEqual(call(port, 'GET', '/ping')[0], 200)
            finally:
                process.terminate()
                process.wait(timeout=10)
            self.assertGreater(recording.stat().st_size, 0)
            self.assertLessEqual(recording.stat().st_size, limit)

    def test_authenticated_or_delegated_server_refuses_recording(self):
        cases = (
            {'MOCK_REQUIRE_AUTH': '1'},
            {'MOCK_YT_UPSTREAM': 'https://yt.internal.example'},
        )
        with tempfile.TemporaryDirectory() as directory:
            for extra_environment in cases:
                with self.subTest(extra_environment=extra_environment):
                    environment = {
                        **clean_environment(),
                        'MOCK_RECORD': str(Path(directory) / 'forbidden.jsonl'),
                        **extra_environment,
                    }
                    process = subprocess.run(
                        [
                            sys.executable,
                            str(BACKEND / 'server.py'),
                            str(free_port()),
                        ],
                        env=environment, capture_output=True, text=True,
                        timeout=10, check=False)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn(
                        'MOCK_RECORD is a development-only fixture',
                        process.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
