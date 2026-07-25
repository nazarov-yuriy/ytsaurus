#!/usr/bin/env python3
"""Sync the structured API catalog (SQLite) with the unstructured docs.

Usage:
    python3 sync.py load    # (re)build api_catalog.sqlite from docs/*.inventory.json
    python3 sync.py export  # regenerate docs/API-INDEX.md from the DB
    python3 sync.py check   # report endpoints not mentioned in any .md doc
    python3 sync.py query "SQL"  # ad-hoc query, prints rows as TSV
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(__file__).resolve().parent / "api_catalog.sqlite"
DOCS = ROOT / "docs"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load() -> None:
    conn = connect()
    conn.executescript((Path(__file__).resolve().parent / "schema.sql").read_text())
    conn.execute("DELETE FROM endpoints")
    conn.execute("DELETE FROM schemas")

    for inv_path in sorted(DOCS.glob("*.inventory.json")):
        entries = json.loads(inv_path.read_text())
        for e in entries:
            # Tolerate schema entries mixed into inventories.
            if e.get("entry_type") == "schema" or ("name" in e and "path" not in e):
                cur = conn.execute(
                    "INSERT OR REPLACE INTO schemas (name, description, definition) VALUES (?, ?, ?)",
                    (e.get("name"), e.get("description"),
                     json.dumps(e.get("definition") or e.get("body_schema"), ensure_ascii=False)),
                )
                for f in e.get("fields", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_fields (schema_id, name, type, required, description)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (cur.lastrowid, f.get("name"), f.get("type"),
                         int(bool(f.get("required"))), f.get("description")),
                    )
                continue

            cur = conn.execute(
                "INSERT OR IGNORE INTO endpoints"
                " (layer, api_version, command, method, path, description, needed_for_mock, source_file)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e.get("layer", "proxy"),
                    e.get("api_version"),
                    e.get("command"),
                    (e.get("method") or "GET").upper(),
                    e.get("path", ""),
                    e.get("description"),
                    int(bool(e.get("needed_for_mock"))),
                    inv_path.name,
                ),
            )
            eid = cur.lastrowid
            if not eid:
                continue

            def add_params(direction: str, obj: dict) -> None:
                if not isinstance(obj, dict):
                    return
                kind_map = {"headers": "header", "params": "param", "query": "query",
                            "body": "body", "cookies": "cookie", "status": "status",
                            "body_schema": "body"}
                for key, kind in kind_map.items():
                    val = obj.get(key)
                    if val is None:
                        continue
                    if isinstance(val, dict):
                        for name, v in val.items():
                            conn.execute(
                                "INSERT OR IGNORE INTO endpoint_params"
                                " (endpoint_id, direction, kind, name, value) VALUES (?, ?, ?, ?, ?)",
                                (eid, direction, kind, str(name),
                                 v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)),
                            )
                    else:
                        conn.execute(
                            "INSERT OR IGNORE INTO endpoint_params"
                            " (endpoint_id, direction, kind, name, value) VALUES (?, ?, ?, ?, ?)",
                            (eid, direction, kind, key,
                             val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)),
                        )

            add_params("request", e.get("request") or {})
            add_params("response", e.get("response") or {})
            for c in e.get("cookies") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO endpoint_params"
                    " (endpoint_id, direction, kind, name, value) VALUES (?, 'request', 'cookie', ?, ?)",
                    (eid, c if isinstance(c, str) else json.dumps(c), None),
                )
            for ref in e.get("source_refs") or []:
                conn.execute("INSERT OR IGNORE INTO source_refs (endpoint_id, ref) VALUES (?, ?)", (eid, ref))

    update_md_coverage(conn)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
    m = conn.execute("SELECT COUNT(*) FROM endpoints WHERE needed_for_mock").fetchone()[0]
    print(f"Loaded {n} endpoints ({m} needed for mock) into {DB_PATH.name}")


def update_md_coverage(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM md_coverage")
    md_files = {p.name: p.read_text() for p in DOCS.glob("*.md") if p.name != "API-INDEX.md"}
    for eid, path, command in conn.execute("SELECT id, path, command FROM endpoints"):
        # Entries may pack several paths into one field ("/a , /b"); any part counts.
        needle_variants = [part.strip() for part in (path or "").split(",") if part.strip()]
        if command:
            needle_variants.append(command)
        for name, text in md_files.items():
            hit = any(n in text for n in needle_variants)
            conn.execute(
                "INSERT OR REPLACE INTO md_coverage (endpoint_id, md_file, mentioned) VALUES (?, ?, ?)",
                (eid, name, int(hit)),
            )


def export() -> None:
    conn = connect()
    out = ["# API Index (generated from db/api_catalog.sqlite — do not edit by hand)", ""]
    for layer, title in (("ui-server", "UI Node server endpoints (browser → UI server)"),
                         ("proxy", "Cluster HTTP proxy endpoints (UI → proxy)")):
        out += [f"## {title}", "",
                "| Mock | Method | Path | Command | Ver | Description |",
                "|------|--------|------|---------|-----|-------------|"]
        rows = conn.execute(
            "SELECT needed_for_mock, method, path, command, api_version, description"
            " FROM endpoints WHERE layer = ? ORDER BY needed_for_mock DESC, path", (layer,))
        for mock, method, path, command, ver, desc in rows:
            desc = re.sub(r"\s+", " ", desc or "").strip()
            out.append(f"| {'✅' if mock else ''} | {method} | `{path}` | {command or ''} | {ver or ''} | {desc} |")
        out.append("")
    schemas = conn.execute("SELECT name, description FROM schemas ORDER BY name").fetchall()
    if schemas:
        out += ["## Payload schemas / type conventions", ""]
        for name, desc in schemas:
            out.append(f"- **{name}** — {re.sub(r'[|]', '/', desc or '')}")
        out.append("")
    (DOCS / "API-INDEX.md").write_text("\n".join(out))
    print(f"Wrote {DOCS / 'API-INDEX.md'}")


def check() -> None:
    conn = connect()
    update_md_coverage(conn)
    conn.commit()
    rows = conn.execute(
        "SELECT e.method, e.path, e.command FROM endpoints e"
        " WHERE NOT EXISTS (SELECT 1 FROM md_coverage c WHERE c.endpoint_id = e.id AND c.mentioned = 1)"
        " ORDER BY e.path").fetchall()
    if not rows:
        print("OK: every endpoint in the DB is mentioned in at least one .md doc")
    else:
        print(f"{len(rows)} endpoints NOT mentioned in any .md doc:")
        for method, path, command in rows:
            print(f"  {method} {path} {command or ''}")


def query(sql: str) -> None:
    conn = connect()
    for row in conn.execute(sql):
        print("\t".join("" if v is None else str(v) for v in row))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "load"
    if cmd == "load":
        load()
    elif cmd == "export":
        export()
    elif cmd == "check":
        check()
    elif cmd == "query":
        query(sys.argv[2])
    else:
        sys.exit(__doc__)
