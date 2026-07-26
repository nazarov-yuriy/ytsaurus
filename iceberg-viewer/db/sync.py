#!/usr/bin/env python3
"""Sync the structured API catalog (SQLite) with the unstructured docs.

Usage:
    python3 sync.py load    # (re)build api_catalog.sqlite from docs/*.inventory.json
    python3 sync.py export  # regenerate docs/API-INDEX.md, ENTITIES.md, INDEX.md
    python3 sync.py check   # report endpoints not mentioned in any .md doc
    python3 sync.py audit   # cross-check catalog statuses vs mock-backend-py/server.py
    python3 sync.py query "SQL"  # ad-hoc query, prints rows as TSV

The DB location can be overridden with API_CATALOG_DB (e.g. for read-only checkouts).
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("API_CATALOG_DB") or Path(__file__).resolve().parent / "api_catalog.sqlite")
DOCS = ROOT / "docs"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_entities(conn: sqlite3.Connection) -> None:
    """Load entity definitions (payload schemas with per-field support status)."""
    here = Path(__file__).resolve().parent
    for src in (here / "entities.json", here / "node-attributes.generated.json"):
        if not src.exists():
            continue
        data = json.loads(src.read_text())
        for e in data if isinstance(data, list) else [data]:
            cur = conn.execute(
                "INSERT OR REPLACE INTO schemas (name, description, definition) VALUES (?, ?, ?)",
                (e["name"], e.get("description"), None))
            for f in e.get("fields", []):
                conn.execute(
                    "INSERT OR REPLACE INTO schema_fields"
                    " (schema_id, name, type, required, support_status, description)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (cur.lastrowid, f["name"], f.get("type"),
                     int(bool(f.get("required"))), f.get("support_status"),
                     f.get("description")))


def apply_support_status(conn: sqlite3.Connection) -> None:
    """Assign support_status to every endpoint via ordered first-match rules."""
    cfg = json.loads((Path(__file__).resolve().parent / "support-status.json").read_text())
    for eid, path, command in conn.execute("SELECT id, path, command FROM endpoints"):
        status = cfg.get("default", "unused")
        for rule in cfg["rules"]:
            if "command" in rule and command == rule["command"]:
                status = rule["status"]
                break
            if "path_prefix_commands" in rule and (command or "").startswith(rule["path_prefix_commands"]):
                status = rule["status"]
                break
            if "path_exact" in rule and path.strip() == rule["path_exact"]:
                status = rule["status"]
                break
            if "path" in rule and path.startswith(rule["path"]):
                status = rule["status"]
                break
        conn.execute("UPDATE endpoints SET support_status = ? WHERE id = ?", (status, eid))


def load() -> None:
    conn = connect()
    # Recreate catalog tables so schema.sql changes take effect (recorded_requests,
    # populated by recordings/analyze.py, is left untouched).
    for table in ("md_coverage", "source_refs", "endpoint_params", "schema_fields",
                  "schemas", "endpoints"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript((Path(__file__).resolve().parent / "schema.sql").read_text())

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

    load_entities(conn)
    apply_support_status(conn)
    update_md_coverage(conn)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
    m = conn.execute("SELECT COUNT(*) FROM endpoints WHERE needed_for_mock").fetchone()[0]
    s = conn.execute("SELECT COUNT(*) FROM schemas").fetchone()[0]
    f = conn.execute("SELECT COUNT(*) FROM schema_fields").fetchone()[0]
    by_status = dict(conn.execute(
        "SELECT support_status, COUNT(*) FROM endpoints GROUP BY support_status"))
    print(f"Loaded {n} endpoints ({m} needed for mock; status: {by_status}),"
          f" {s} entities with {f} fields into {DB_PATH.name}")


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


STATUS_BADGE = {"implemented": "🟢 implemented", "constant": "🟡 constant", "unused": "⚪ unused"}


def export() -> None:
    conn = connect()
    out = ["# API Index (generated from db/api_catalog.sqlite — do not edit by hand)", "",
           "Support status: 🟢 implemented = dynamic behavior backed by in-RAM data"
           " (reimplement over an Iceberg catalog); 🟡 constant = fixed/stubbed response"
           " (keep as-is); ⚪ unused = not needed for the Iceberg viewer.", ""]
    for layer, title in (("ui-server", "UI Node server endpoints (browser → UI server)"),
                         ("proxy", "Cluster HTTP proxy endpoints (UI → proxy)")):
        out += [f"## {title}", "",
                "| Status | Method | Path | Command | Ver | Description |",
                "|--------|--------|------|---------|-----|-------------|"]
        rows = conn.execute(
            "SELECT support_status, method, path, command, api_version, description"
            " FROM endpoints WHERE layer = ? ORDER BY"
            " CASE support_status WHEN 'implemented' THEN 0 WHEN 'constant' THEN 1 ELSE 2 END, path",
            (layer,))
        for status, method, path, command, ver, desc in rows:
            desc = re.sub(r"\s+", " ", desc or "").strip()
            out.append(f"| {STATUS_BADGE.get(status, '')} | {method} | `{path}` |"
                       f" {command or ''} | {ver or ''} | {desc} |")
        out.append("")
    (DOCS / "API-INDEX.md").write_text("\n".join(out))

    ent = ["# Entities (generated from db/api_catalog.sqlite — do not edit by hand)", "",
           "Every payload entity and every field, with mock support status:"
           " 🟢 implemented (dynamic, reimplement over Iceberg), 🟡 constant (stubbed,"
           " keep as-is), ⚪ unused (documented, not needed by the viewer).", ""]
    for sid, name, desc in conn.execute("SELECT id, name, description FROM schemas ORDER BY name"):
        ent += [f"## {name}", "", re.sub(r"\s+", " ", desc or "").strip(), "",
                "| Status | Field | Type | Required | Description |",
                "|--------|-------|------|----------|-------------|"]
        for fname, ftype, req, fstatus, fdesc in conn.execute(
                "SELECT name, type, required, support_status, description FROM schema_fields"
                " WHERE schema_id = ? ORDER BY"
                " CASE support_status WHEN 'implemented' THEN 0 WHEN 'constant' THEN 1 ELSE 2 END, name",
                (sid,)):
            fdesc = re.sub(r"[|]", "/", re.sub(r"\s+", " ", fdesc or "")).strip()
            ftype = re.sub(r"[|]", "/", ftype or "")
            ent.append(f"| {STATUS_BADGE.get(fstatus, '')} | `{fname}` | {ftype} |"
                       f" {'yes' if req else ''} | {fdesc} |")
        ent.append("")
    (DOCS / "ENTITIES.md").write_text("\n".join(ent))
    doc_index()
    print(f"Wrote {DOCS / 'API-INDEX.md'}, {DOCS / 'ENTITIES.md'} and {DOCS / 'INDEX.md'}")


GENERATED_DOCS = {"docs/API-INDEX.md", "docs/ENTITIES.md", "docs/INDEX.md",
                  "recordings/COVERAGE.md"}
DOC_GROUPS = [
    ("Start here", ["README.md"]),
    ("Protocol documentation (handwritten)", [
        "docs/auth.md", "docs/table-viewer.md", "docs/bootstrap-config.md",
        "docs/empirical-findings.md", "docs/coverage-notes.md", "docs/timeouts.md"]),
    ("Planning", ["docs/iceberg-considerations.md"]),
    ("Reviews", ["docs/architecture-review.md", "docs/security-review.md", "docs/proxy-test-coverage.md"]),
    ("Generated from the SQLite catalog / recordings — do not edit", [
        "docs/API-INDEX.md", "docs/ENTITIES.md", "recordings/COVERAGE.md"]),
    ("Component guides", [
        "mock-backend-py/README.md", "recordings/README.md", "deploy/README.md"]),
]


def doc_index() -> None:
    """Generate docs/INDEX.md: every doc, its purpose, and doc<->doc cross-references."""
    all_docs = sorted({
        str(p.relative_to(ROOT))
        for pattern in ("*.md", "docs/*.md", "*/README.md", "recordings/*.md")
        for p in ROOT.glob(pattern)
        if "ytsaurus-ui" not in str(p)} - {"docs/INDEX.md"})  # the index itself stays out of the graph
    texts = {d: (ROOT / d).read_text() for d in all_docs}

    def title(doc):
        m = re.search(r"^# (.+)$", texts[doc], re.M)
        return (m.group(1) if m else doc).strip()

    # A doc references another when it mentions its relative path, or its bare
    # basename when that basename is unique (READMEs need the qualified path).
    basenames = {}
    for d in all_docs:
        basenames.setdefault(Path(d).name, []).append(d)
    outgoing = {d: set() for d in all_docs}
    for d, text in texts.items():
        for other in all_docs:
            if other == d:
                continue
            needles = [other]
            name = Path(other).name
            if len(basenames[name]) == 1:
                needles.append(name)
            if any(n in text for n in needles):
                outgoing[d].add(other)
    incoming = {d: sorted(o for o, outs in outgoing.items() if d in outs) for d in all_docs}

    def link(doc):
        return f"[{doc}](../{doc})"

    out = ["# Documentation index (generated by db/sync.py — do not edit by hand)", "",
           "Which doc covers what, and which doc mentions which. Regenerate with"
           " `python3 db/sync.py export`; `check` fails when this file is stale.", ""]
    listed = [d for _, docs in DOC_GROUPS for d in docs]
    groups = DOC_GROUPS + ([("Ungrouped", [d for d in all_docs if d not in listed])]
                           if any(d not in listed for d in all_docs) else [])
    for group, docs in groups:
        out += [f"## {group}", ""]
        for d in docs:
            if d not in texts:
                continue
            tag = " *(generated)*" if d in GENERATED_DOCS else ""
            out.append(f"### {link(d)}{tag}")
            out.append(f"*{title(d)}*")
            if outgoing[d]:
                out.append("- mentions: " + ", ".join(link(o) for o in sorted(outgoing[d])))
            if incoming[d]:
                out.append("- mentioned by: " + ", ".join(link(o) for o in incoming[d]))
            if not outgoing[d] and not incoming[d]:
                out.append("- no cross-references")
            out.append("")
    (DOCS / "INDEX.md").write_text("\n".join(out))


def check() -> None:
    conn = connect()
    # Refresh coverage inside this connection for accurate checks, but roll it
    # back before exit so a sanity check does not rewrite the tracked database.
    update_md_coverage(conn)
    failures = 0

    rows = conn.execute(
        "SELECT e.method, e.path, e.command FROM endpoints e"
        " WHERE NOT EXISTS (SELECT 1 FROM md_coverage c WHERE c.endpoint_id = e.id AND c.mentioned = 1)"
        " ORDER BY e.path").fetchall()
    if not rows:
        print("OK: every endpoint in the DB is mentioned in at least one .md doc")
    else:
        failures += len(rows)
        print(f"{len(rows)} endpoints NOT mentioned in any .md doc:")
        for method, path, command in rows:
            print(f"  {method} {path} {command or ''}")

    missing_status = conn.execute(
        "SELECT COUNT(*) FROM endpoints WHERE support_status IS NULL").fetchone()[0]
    missing_field_status = conn.execute(
        "SELECT COUNT(*) FROM schema_fields WHERE support_status IS NULL").fetchone()[0]
    if missing_status or missing_field_status:
        failures += missing_status + missing_field_status
        print(f"{missing_status} endpoints / {missing_field_status} fields without support_status")
    else:
        print("OK: every endpoint and every entity field has a support_status")

    # Generated MD must be in sync with the DB (regenerate-and-compare).
    import io
    from contextlib import redirect_stdout
    before = {p.name: p.read_text()
              for p in (DOCS / "API-INDEX.md", DOCS / "ENTITIES.md", DOCS / "INDEX.md")
              if p.exists()}
    with redirect_stdout(io.StringIO()):
        export()
    for name, old in before.items():
        if (DOCS / name).read_text() != old:
            failures += 1
            print(f"STALE: {name} was out of date with the DB (now regenerated — commit it)")
    if before and all((DOCS / n).read_text() == o for n, o in before.items()):
        print("OK: generated API-INDEX.md, ENTITIES.md and INDEX.md are up to date")

    conn.rollback()
    conn.close()
    sys.exit(1 if failures else 0)


def audit() -> None:
    """Cross-check the catalog's implemented/constant claims against the actual
    backend source (mock-backend-py/server.py): every route and command the
    server dispatches must exist in the DB with a non-'unused' status, and every
    non-'unused' proxy /api command in the DB must exist in the server."""
    server = (ROOT / "mock-backend-py" / "server.py").read_text()
    served_paths = set(re.findall(r"if p == '(/[^']*)'", server))
    served_paths |= {p for group in re.findall(r"if p in \(([^)]*)\)", server)
                     for p in re.findall(r"'(/[^']*)'", group)}
    for prefix in re.findall(r"p\.startswith\('(/[^']+?)/?'\)", server):
        served_paths.add(prefix.rstrip("/"))
    commands_block = server.split("COMMANDS = {", 1)[1].split("\n}", 1)[0]
    served_commands = set(re.findall(r"^    '(\w+)':", commands_block, re.M))

    conn = connect()
    failures = 0
    db_paths = {path.strip() for row in conn.execute(
        "SELECT path FROM endpoints WHERE layer='proxy' AND support_status != 'unused'")
        for path in row[0].split(",")}
    db_commands = {c for (c,) in conn.execute(
        "SELECT DISTINCT command FROM endpoints WHERE layer='proxy'"
        " AND support_status != 'unused' AND command IS NOT NULL")}

    def covered(path):
        candidates = {path, path.rstrip("/") or "/"}
        return candidates & db_paths or any(d.endswith("*") and path.startswith(d[:-1])
                                            for d in db_paths)

    for path in sorted(served_paths):
        if not covered(path):
            failures += 1
            print(f"NOT IN CATALOG: server route {path}")
    for command in sorted(served_commands):
        if not any(f"/api/v3/{command}" in d or f"/api/v4/{command}" in d for d in db_paths) \
                and command not in db_commands:
            failures += 1
            print(f"NOT IN CATALOG: server command {command}")
    for (path, command) in conn.execute(
            "SELECT path, command FROM endpoints WHERE layer='proxy'"
            " AND support_status='implemented' AND path LIKE '/api/v_/%'"
            " AND path NOT LIKE '%*%' AND path NOT LIKE '%:%'"):
        for part in path.split(","):
            name = part.strip().rsplit("/", 1)[-1]
            if name and name not in served_commands and not (command or "").startswith("execute_batch:"):
                failures += 1
                print(f"NOT IN SERVER: catalog claims implemented {part.strip()}")
    print("OK: catalog matches the server surface" if not failures
          else f"{failures} drift(s) between catalog and server")
    sys.exit(1 if failures else 0)


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
    elif cmd == "audit":
        audit()
    elif cmd == "query":
        query(sys.argv[2])
    else:
        sys.exit(__doc__)
