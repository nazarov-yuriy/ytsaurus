#!/usr/bin/env python3
"""Differential test: replay recorded traffic against the Node and Python mock
backends and diff status / body / key headers.

Usage: python3 replay-diff.py  (starts Node on 8001 and Python on 8002 itself)
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NODE_PORT, PY_PORT = 8001, 8002


def start_servers():
    node = subprocess.Popen(['node', str(ROOT / 'mock-backend' / 'server.js'), str(NODE_PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    py = subprocess.Popen([sys.executable, str(ROOT / 'mock-backend-py' / 'server.py'), str(PY_PORT)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for port in (NODE_PORT, PY_PORT):
        for _ in range(50):
            try:
                urllib.request.urlopen(f'http://localhost:{port}/ping', timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f'server on {port} did not start')
    return node, py


def send(port, entry):
    url = f'http://localhost:{port}{entry["path"]}{entry.get("query", "")}'
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
    return {'status': status, 'body': parsed,
            'yt_code': hdrs.get('X-YT-Response-Code'),
            'content_type': (hdrs.get('Content-Type') or '').split(';')[0]}


EDGE_CASES = [
    {'method': 'POST', 'path': '/login', 'request_headers': {'Authorization': 'Basic aWNlYmVyZzppY2ViZXJn'}},   # iceberg:iceberg
    {'method': 'POST', 'path': '/login', 'request_headers': {'Authorization': 'Basic aWNlYmVyZzp3cm9uZw=='}},   # wrong password
    {'method': 'POST', 'path': '/login'},                                                                        # no auth at all
    {'method': 'POST', 'path': '/api/v4/list', 'request_body': {'path': '//home/iceberg/warehouse'}},
    {'method': 'POST', 'path': '/api/v4/exists', 'request_body': {'path': '//home/iceberg/warehouse/trips'}},
    {'method': 'POST', 'path': '/api/v4/exists', 'request_body': {'path': '//nope'}},
    {'method': 'POST', 'path': '/api/v3/get', 'request_body': {'path': '//home/iceberg/warehouse/trips/@schema'}},
    {'method': 'POST', 'path': '/api/v3/get', 'request_body': {'path': '//home/iceberg/warehouse/trips/@schema/0/name'}},
    {'method': 'POST', 'path': '/api/v3/get', 'request_body': {'path': '//home/iceberg/warehouse/trips/@nonexistent_attr'}},
    {'method': 'POST', 'path': '/api/v3/get', 'request_body': {'path': '//totally/absent'}},
    {'method': 'POST', 'path': '/api/v3/list', 'request_body': {'path': '//home/iceberg/warehouse/trips'}},      # list on a table
    {'method': 'POST', 'path': '/api/v3/read_table',
     'request_body': {'path': '//home/iceberg/warehouse/trips[#240:#260]',
                      'output_format': {'$value': 'web_json', '$attributes': {'max_selected_column_count': 2}}}},
    {'method': 'POST', 'path': '/api/v3/read_table',
     'request_body': {'path': '//home/iceberg/warehouse/events[#0:#5]',
                      'output_format': {'$value': 'web_json',
                                        '$attributes': {'column_names': ['ts', 'payload'], 'max_all_column_names_count': 2}}}},
    {'method': 'POST', 'path': '/api/v3/read_table',
     'request_body': {'path': '//home/iceberg/warehouse/trips[#0:#2]', 'output_format': 'json'}},
    {'method': 'POST', 'path': '/api/v3/read_table', 'request_body': {'path': '//home/iceberg/warehouse'}},      # not a table
    {'method': 'POST', 'path': '/api/v3/execute_batch',
     'request_body': {'requests': [{'command': 'get', 'parameters': {'path': '//home/iceberg/warehouse/@'}},
                                   {'command': 'nope', 'parameters': {}},
                                   {'command': 'get', 'parameters': {'path': '//bad'}}],
                      'output_format': {'$value': 'json', '$attributes': {'annotate_with_types': True, 'stringify': True}}}},
    {'method': 'POST', 'path': '/api/v3/get_table_columnar_statistics',
     'request_body': {'paths': ['//home/iceberg/warehouse/trips', '//home/iceberg/warehouse/events']}},
    {'method': 'POST', 'path': '/api/v3/frobnicate', 'request_body': {}},                                        # unknown command
    {'method': 'GET', 'path': '/definitely/not/a/route'},
    {'method': 'GET', 'path': '/api/v3/get', 'query': '?path=//home/iceberg/warehouse/trips/@row_count'},        # query-string params
    {'method': 'POST', 'path': '/api/v3/get', 'request_headers':
        {'X-YT-Parameters': '{"path":"//home/iceberg/warehouse/trips/@type"}'}},                                  # header params
    {'method': 'GET', 'path': '/auth/whoami', 'request_headers': {'Authorization': 'OAuth sometoken'}},
    {'method': 'GET', 'path': '/hosts'},
    {'method': 'GET', 'path': '/hosts/all'},
    {'method': 'GET', 'path': '/api/'},
    {'method': 'GET', 'path': '/version'},
]

SKIP_BODY_PATHS = {'/login'}  # Set-Cookie is random; body is empty — status compare only


def main():
    node, py = start_servers()
    try:
        entries = [json.loads(line) for line in (HERE / 'proxy-traffic.jsonl').read_text().splitlines()]
        entries += EDGE_CASES
        # /hosts returns the host:port of the serving proxy — normalize.
        def normalize(x, port):
            if isinstance(x, list):
                return [normalize(i, port) for i in x]
            if isinstance(x, dict):
                return {k: normalize(v, port) for k, v in x.items()}
            if isinstance(x, str):
                # CSRF tokens are HMACs over per-process random secrets + a
                # timestamp — mask them so the two backends stay comparable.
                x = re.sub(r'^[0-9a-f]{64}:\d+$', '<csrf>', x)
                return x.replace(f'localhost:{port}', 'localhost:PORT')
            return x

        mismatches = 0
        for i, e in enumerate(entries):
            a = send(NODE_PORT, e)
            b = send(PY_PORT, e)
            a['body'] = normalize(a['body'], NODE_PORT)
            b['body'] = normalize(b['body'], PY_PORT)
            fields = ['status', 'yt_code', 'content_type'] + \
                     ([] if e['path'] in SKIP_BODY_PATHS else ['body'])
            diffs = [f for f in fields if a[f] != b[f]]
            if diffs:
                mismatches += 1
                print(f'MISMATCH #{i} {e["method"]} {e["path"]}{e.get("query","")} on {diffs}')
                for f in diffs:
                    print(f'  node: {json.dumps(a[f])[:400]}')
                    print(f'  py:   {json.dumps(b[f])[:400]}')
        total = len(entries)
        print(f'{total - mismatches}/{total} identical ({len(EDGE_CASES)} edge cases included)')
        sys.exit(1 if mismatches else 0)
    finally:
        node.terminate()
        py.terminate()


if __name__ == '__main__':
    main()
