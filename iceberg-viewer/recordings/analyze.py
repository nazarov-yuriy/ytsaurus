#!/usr/bin/env python3
"""Build a request corpus from the recorded play session and diff it against the
documented API catalog.

Inputs:  proxy-traffic.jsonl (mock-side), browser-traffic.har (browser-side)
Outputs: corpus.json           - one representative example per distinct request shape
         COVERAGE.md           - two-way diff vs db/api_catalog.sqlite
         recorded_requests table in the SQLite DB
"""
import json
import re
import sqlite3
from collections import OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parent / "db" / "api_catalog.sqlite"


def norm_path(p: str) -> str:
    p = re.sub(r"//home/iceberg/\S*?(?=[\"\s]|$)", "<ypath>", p)
    p = re.sub(r"\[#\d+:#\d+\]", "[#a:#b]", p)
    return p


def signature(entry):
    """Distinct shape: method + path + command details (incl. batch subcommands)."""
    m, p = entry["method"], entry["path"]
    body = entry.get("request_body")
    parts = [m, p]
    if isinstance(body, dict):
        if "requests" in body:  # execute_batch
            subs = []
            for r in body.get("requests", []):
                params = r.get("parameters", {})
                path = str(params.get("path", ""))
                attr = ""
                if "/@" in path:
                    attr = "@" + path.split("/@", 1)[1].split("/", 1)[0]
                    attr = attr if len(attr) > 1 else "@(all)"
                subs.append(f"{r.get('command')}{':' + attr if attr else ''}")
            parts.append("batch[" + ",".join(sorted(set(subs))) + "]")
        else:
            keys = ",".join(sorted(k for k in body if k not in ("path",)))
            path = str(body.get("path", ""))
            if "/@" in path:
                attr = path.split("/@", 1)[1].split("/", 1)[0]
                parts.append(f"@{attr or '(all)'}")
            if keys:
                parts.append(f"params({keys})")
            of = body.get("output_format")
            if isinstance(of, dict):
                parts.append(f"of={of.get('$value')}")
    return " ".join(parts)


def load_proxy_entries():
    out = []
    with open(HERE / "proxy-traffic.jsonl") as f:
        for line in f:
            out.append(json.loads(line))
    return out


def load_browser_entries():
    # Prefer the compact pre-extracted JSONL (the raw HAR embeds JS bundles, ~125MB).
    jsonl = HERE / "browser-traffic.jsonl"
    if jsonl.exists() and not (HERE / "browser-traffic.har").exists():
        return [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    har = json.loads((HERE / "browser-traffic.har").read_text())
    out = []
    for e in har["log"]["entries"]:
        url = e["request"]["url"]
        m = re.match(r"https?://[^/]+(/api/[^?]*)", url)
        if not m:
            continue
        body = None
        post = e["request"].get("postData", {}).get("text")
        if post:
            try:
                body = json.loads(post)
            except ValueError:
                body = post[:2000]
        out.append({
            "method": e["request"]["method"],
            "path": m.group(1),
            "query": (url.split("?", 1)[1][:500] if "?" in url else ""),
            "request_body": body,
            "status": e["response"]["status"],
            "response_body": None,
        })
    return out


def load_extras():
    """Manually-recorded browser-layer requests (curl extras), one JSON per line."""
    path = HERE / "extras.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    proxy = load_proxy_entries()
    browser = load_browser_entries() + load_extras()

    corpus = OrderedDict()
    for layer, entries in (("proxy", proxy), ("browser", browser)):
        for e in entries:
            sig = f"[{layer}] {signature({**e, 'path': norm_path(e['path'])})}"
            item = corpus.setdefault(sig, {"count": 0, "statuses": set(), "example": e})
            item["count"] += 1
            item["statuses"].add(e["status"])

    (HERE / "corpus.json").write_text(json.dumps(
        [{"signature": k, "count": v["count"], "statuses": sorted(v["statuses"]),
          "example": v["example"]} for k, v in corpus.items()],
        indent=1, ensure_ascii=False))

    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS recorded_requests (
        id INTEGER PRIMARY KEY, layer TEXT, signature TEXT UNIQUE, method TEXT,
        path TEXT, count INTEGER, statuses TEXT, example_json TEXT)""")
    conn.execute("DELETE FROM recorded_requests")
    for sig, v in corpus.items():
        layer = "proxy" if sig.startswith("[proxy]") else "ui-server"
        conn.execute(
            "INSERT OR REPLACE INTO recorded_requests"
            " (layer, signature, method, path, count, statuses, example_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (layer, sig, v["example"]["method"], v["example"]["path"], v["count"],
             ",".join(str(s) for s in sorted(v["statuses"])),
             json.dumps(v["example"], ensure_ascii=False)[:20000]))
    conn.commit()

    # --- two-way coverage diff ------------------------------------------------
    def doc_key(layer, method, path):
        return (layer, path.split("?")[0])

    documented = {}
    for layer, method, path, command, needed in conn.execute(
            "SELECT layer, method, path, command, needed_for_mock FROM endpoints"):
        for part in path.split(","):
            documented.setdefault((layer, part.strip()), []).append((command, bool(needed)))

    hit_proxy_paths = {e["path"] for e in proxy}
    hit_browser_paths = {re.sub(r"/mock(/|$)", r"/:cluster\1", e["path"]) for e in browser}

    def match_documented(layer, hits):
        seen, unseen = [], []
        for (l, p), cmds in documented.items():
            if l != layer:
                continue
            pattern = re.escape(p)
            pattern = re.sub(r":\w+|\\\*", "[^/]+", pattern.replace("\\:", ":"))
            pattern = re.sub(r"\\:[a-zA-Z?]+", "[^/]+", pattern)
            rx = re.compile("^" + pattern.replace("[^/]+\\?", "[^/]*") + "$")
            if any(rx.match(h) for h in hits):
                seen.append((p, cmds))
            else:
                unseen.append((p, cmds))
        return seen, unseen

    proxy_seen, proxy_unseen = match_documented("proxy", hit_proxy_paths)
    ui_seen, ui_unseen = match_documented("ui-server", hit_browser_paths)

    lines = ["# Play-session coverage vs documented API catalog", "",
             f"Recorded: {len(proxy)} proxy-side requests, {len(browser)} browser-side"
             f" requests, {len(corpus)} distinct shapes (see corpus.json).", ""]

    lines += ["## Distinct request shapes recorded", ""]
    for sig, v in sorted(corpus.items()):
        lines.append(f"- `{sig}` ×{v['count']} → {sorted(v['statuses'])}")

    for title, seen, unseen in (("Proxy endpoints", proxy_seen, proxy_unseen),
                                ("UI-server endpoints", ui_seen, ui_unseen)):
        lines += ["", f"## {title}: documented but NOT exercised", ""]
        needed = [(p, c) for p, c in unseen if any(n for _, n in c)]
        optional = [(p, c) for p, c in unseen if not any(n for _, n in c)]
        lines.append(f"Mock-critical ({len(needed)}):")
        for p, cmds in sorted(needed):
            lines.append(f"- `{p}` ({', '.join(sorted(set(c or '' for c, _ in cmds)))})")
        lines.append("")
        lines.append(f"Out-of-scope/optional ({len(optional)}): " +
                     ", ".join(f"`{p}`" for p, _ in sorted(optional)))
        lines += ["", f"## {title}: exercised ({len(seen)})", ""]
        for p, _ in sorted(seen):
            lines.append(f"- `{p}`")

    lines += ["", "## Notes", "",
              "- HTML page routes (`/`, `/:ytAuthCluster/...`) listed as unexercised were in fact "
              "loaded by the play session; the HAR filter only keeps `/api/*` requests, so they "
              "never enter the hit set. Treat them as covered by any page navigation.",
              "- `/ready` is a deployment readiness endpoint, not UI traffic; Helm probes and "
              "backend tests exercise it outside this recorded play session.",
              "- `POST /api/yt/logout` returns 404 because the logout route is only mounted when "
              "the UI server's auth policy is enabled; with `authentication: \"none\"` there is "
              "no session to destroy.",
              "- `POST /api/yt/mock/login` succeeds even in auth-none mode: the UI server "
              "forwards Basic auth to the proxy `/login`, which sets `YTCypressCookie`.",
              "- Batch-level errors (e.g. nonexistent paths) travel inside HTTP-200 "
              "`execute_batch` responses as per-item `{error}` objects — HTTP status stays 200."]

    (HERE / "COVERAGE.md").write_text("\n".join(lines) + "\n")
    print(f"corpus: {len(corpus)} shapes; proxy hits: {len(hit_proxy_paths)} paths;"
          f" browser hits: {len(hit_browser_paths)} paths")
    print("wrote corpus.json, COVERAGE.md, table recorded_requests")


if __name__ == "__main__":
    main()
