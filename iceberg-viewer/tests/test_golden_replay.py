#!/usr/bin/env python3
"""Golden replay of the recorded UI corpus against the backend.

Replays every request in recordings/proxy-traffic.jsonl (the traffic a real UI
session produced) and compares status, X-YT-Response-Code, Content-Type and
body against recordings/golden.jsonl. This is the wire contract with the UI:
run it after backend changes, before swapping data.py for a real Iceberg
catalog, and after ytsaurus-ui image upgrades (re-record the corpus first in
that case — see recordings/README.md).

Run:                   python3 tests/test_golden_replay.py
Regenerate golden:     GOLDEN_UPDATE=1 python3 tests/test_golden_replay.py

Nondeterministic values are normalized before comparison: CSRF tokens (random
secret + timestamp), the /hosts self-address, and /login (empty body, random
Set-Cookie — status/headers-only comparison).
"""
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
CORPUS = ROOT / 'recordings' / 'proxy-traffic.jsonl'
GOLDEN = ROOT / 'recordings' / 'golden.jsonl'
PORT = 8061
UPDATE = bool(os.environ.get('GOLDEN_UPDATE'))

SKIP_BODY_PATHS = {'/login'}  # empty body; Set-Cookie is random

_proc = None


def setUpModule():
    global _proc
    env = {k: v for k, v in os.environ.items()
           if k not in (
               'MOCK_DELAY',
               'MOCK_ENABLE_DEV_SEED_USERS',
               'MOCK_PG_DSN',
               'MOCK_REQUIRE_AUTH',
               'MOCK_ROBOT_TOKEN',
           )}
    env['MOCK_ENABLE_DEV_SEED_USERS'] = '1'
    _proc = subprocess.Popen([sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(PORT)],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f'http://localhost:{PORT}/ping', timeout=1)
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('backend did not start')


def tearDownModule():
    _proc.terminate()


def send(entry):
    url = f'http://localhost:{PORT}{entry["path"]}{entry.get("query", "")}'
    body = entry.get('request_body')
    data = None
    headers = dict(entry.get('request_headers') or {})
    if body is not None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        headers.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=entry.get('method', 'GET'))
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        raw, status, hdrs = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, e.headers
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode('utf-8', 'replace')
    return {'status': status,
            'yt_code': hdrs.get('X-YT-Response-Code'),
            'content_type': (hdrs.get('Content-Type') or '').split(';')[0],
            'body': normalize(parsed)}


def normalize(x):
    if isinstance(x, list):
        return [normalize(i) for i in x]
    if isinstance(x, dict):
        return {k: normalize(v) for k, v in x.items()}
    if isinstance(x, str):
        x = re.sub(r'^[0-9a-f]{64}:\d+$', '<csrf>', x)  # signed CSRF tokens
        return x.replace(f'localhost:{PORT}', 'localhost:<port>')
    return x


def observe(entry):
    result = send(entry)
    if entry['path'] in SKIP_BODY_PATHS:
        result['body'] = '<skipped>'
    return result


def corpus_entries():
    return [json.loads(line) for line in CORPUS.read_text().splitlines()]


class TestGoldenReplay(unittest.TestCase):
    def test_corpus_matches_golden(self):
        entries = corpus_entries()
        if UPDATE:
            with open(GOLDEN, 'w') as f:
                for entry in entries:
                    f.write(json.dumps(observe(entry), ensure_ascii=False) + '\n')
            self.skipTest(f'golden regenerated: {len(entries)} responses -> {GOLDEN.name}')
        self.assertTrue(GOLDEN.exists(),
                        'recordings/golden.jsonl missing — run GOLDEN_UPDATE=1 once')
        golden = [json.loads(line) for line in GOLDEN.read_text().splitlines()]
        self.assertEqual(len(golden), len(entries),
                         'corpus and golden diverge in length — regenerate golden')
        mismatches = 0
        for i, (entry, expected) in enumerate(zip(entries, golden)):
            with self.subTest(i=i, request=f'{entry["method"]} {entry["path"]}'):
                actual = observe(entry)
                if actual != expected:
                    mismatches += 1
                self.assertEqual(actual, expected)
        if not mismatches:
            print(f'\n{len(entries)}/{len(entries)} corpus responses match golden', file=sys.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=1)
