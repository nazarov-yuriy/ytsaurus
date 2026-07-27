#!/usr/bin/env python3
"""End-to-end smoke for the anonymous Compose stack.

Mirrors the anonymous Helm test pod and additionally verifies /ready. Uses
only urllib, so the smoke check itself needs no extra packages.

Env: UI_URL (default http://ui), MOCK_URL (default http://mock-backend:8000).
"""
import json
import os
import re
import sys
import urllib.request

UI = os.environ.get('UI_URL', 'http://ui').rstrip('/')
MOCK = os.environ.get('MOCK_URL', 'http://mock-backend:8000').rstrip('/')
CLUSTER = 'mock'


def get(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'} if data else {},
        method='POST' if data else 'GET')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode()


def check(name, condition):
    if not condition:
        sys.exit(f'FAILED: {name}')
    print(f'ok: {name}')


check('mock /version answers with a semver',
      re.search(r'\d+\.\d+\.\d+', get(f'{MOCK}/version')))
check('mock /auth/whoami returns a csrf token',
      'csrf_token' in get(f'{MOCK}/auth/whoami'))
check('mock /ready reports storage reachable',
      get(f'{MOCK}/ready') == '{}')
check('UI serves the app shell',
      len(get(f'{UI}/{CLUSTER}/navigation')) > 0)
info = json.loads(get(f'{UI}/api/cluster-info/{CLUSTER}'))
check('UI cluster-info gate (version + csrf_token)',
      info.get('version') and info.get('token', {}).get('csrf_token'))
params = json.loads(get(f'{UI}/api/cluster-params/{CLUSTER}'))
check('UI cluster-params gate (mediumList without error)',
      'output' in params.get('mediumList', {}))
check('exists through the UI tunnel',
      get(f'{UI}/api/yt/{CLUSTER}/api/v3/exists',
          {'path': '//home/iceberg/warehouse'}) == 'true')
rows = json.loads(get(
    f'{UI}/api/yt/{CLUSTER}/api/v3/read_table',
    {'path': '//home/iceberg/warehouse/trips[#0:#3]',
     'output_format': {'$value': 'web_json', '$attributes': {'max_selected_column_count': 50}}}))
check('read_table web_json returns rows through the tunnel',
      len(rows['rows']) == 3 and rows['all_column_names'])

print('ALL SMOKE CHECKS PASSED')
