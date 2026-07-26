#!/usr/bin/env python3
"""Turn recorded-but-uncataloged proxy calls into catalog inventory stubs.

Reads every recordings/*-traffic.jsonl, finds proxy requests whose path/command
has no entry in the API catalog, and (re)writes docs/discovered.inventory.json
with one stub per discovery — observed statuses included, support status left
to db/support-status.json (default: unused). Deterministic output: re-running
without new traffic produces no diff.

Run after recording a new session; then `python3 ../db/sync.py load && check && audit`.
"""
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / 'docs' / 'discovered.inventory.json'


def catalog_surface():
    conn = sqlite3.connect(ROOT / 'db' / 'api_catalog.sqlite')
    paths, commands = set(), set()
    # Exclude our own stubs so regeneration is a stable fixpoint of the traffic.
    for (path,) in conn.execute(
            "SELECT path FROM endpoints WHERE layer='proxy'"
            " AND source_file != 'discovered.inventory.json'"):
        for part in path.split(','):
            part = part.strip()
            if '*' not in part and ':' not in part:
                paths.add(part.rstrip('/') or '/')
            m = re.fullmatch(r'/api/v\d/(\w+)', part)
            if m:
                commands.add(m.group(1))
    return paths, commands


def observed_requests():
    """(kind, name) -> {statuses, example}; kind is 'path' or 'command'."""
    seen = defaultdict(lambda: {'statuses': set(), 'example': None})
    for traffic in sorted(HERE.glob('*-traffic.jsonl')):
        if traffic.name.startswith('browser-'):
            continue  # browser->UI-server layer; parameterized routes live in the catalog
        for line in traffic.read_text().splitlines():
            e = json.loads(line)
            m = re.fullmatch(r'/api/(v\d)/(\w+)', e['path'])
            key = ('command', f'{m.group(1)}/{m.group(2)}') if m else ('path', e['path'].rstrip('/') or '/')
            entry = seen[key]
            entry['statuses'].add(e['status'])
            if entry['example'] is None:
                entry['example'] = e
    return seen


def main():
    paths, commands = catalog_surface()
    stubs = []
    for (kind, name), info in sorted(observed_requests().items()):
        if kind == 'command' and name.split('/', 1)[1] not in commands:
            e = info['example']
            version, command = name.split('/', 1)
            stubs.append({
                'layer': 'proxy',
                'method': e['method'],
                'path': f'/api/{version}/{command}',
                'command': command,
                'api_version': version,
                'description': f'Discovered in recorded UI traffic (statuses {sorted(info["statuses"])}); '
                               'not implemented by the mock — the UI degrades gracefully. '
                               'Promote to a handwritten inventory when implementing.',
                'request': {'params': {k: '...' for k in (e.get('request_body') or {})
                            if isinstance(e.get('request_body'), dict)}},
                'response': {'body_schema': 'unknown (unimplemented)'},
                'needed_for_mock': False,
                'source_refs': ['recordings/discovery-traffic.jsonl'],
            })
        elif kind == 'path' and name not in paths:
            stubs.append({
                'layer': 'proxy',
                'method': info['example']['method'],
                'path': name,
                'description': f'Discovered in recorded UI traffic (statuses {sorted(info["statuses"])}); '
                               'uncataloged route. Promote to a handwritten inventory once understood.',
                'request': {}, 'response': {'body_schema': 'unknown'},
                'needed_for_mock': False,
                'source_refs': ['recordings/discovery-traffic.jsonl'],
            })
    OUT.write_text(json.dumps(stubs, indent=2, ensure_ascii=False) + '\n')
    print(f'{len(stubs)} uncataloged call(s) -> {OUT.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
