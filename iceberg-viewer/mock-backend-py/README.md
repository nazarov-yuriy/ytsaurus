# Mock YTsaurus HTTP proxy — Python implementation

Python (stdlib-only) port of `../mock-backend/` (Node), byte-compatible on the wire.

## Run

```bash
python3 server.py 8000
# then point ytsaurus-ui's clusters-config.json at localhost:8000, same as the Node mock
```

Env vars: `MOCK_RECORD=<path>` appends request/response JSONL (same format as Node).

## Files (1:1 with the Node implementation)

- `server.py` ← `server.js` — routing, auth (Basic `/login`, `YTCypressCookie`
  sessions, CSRF, anonymous fallback), command dispatch, error envelopes,
  v4 `{value}` wrapping, typed annotation.
- `data.py` ← `data.js` — the in-RAM cluster. Node-id sequence, timestamps, and
  generated rows are identical to the Node version. **Swap this file for an
  Apache Iceberg catalog implementation; everything else stays.**
- `webjson.py` ← `webjson.js` — annotated JSON, typed annotation, web_json.
  Includes JS-compatible number stringification (`3`, not `3.0`).

## Consistency guarantees

Verified equivalent to the Node backend by:

1. `../recordings/replay-diff.py` — replays all 165 recorded UI requests plus 26
   edge cases against both servers side by side and diffs status, body, and YT
   headers: **191/191 identical**.
2. `../tests/test_protocol.py` — 30 documented-behavior conformance tests run
   against both backends.
3. Headless-Chromium runs of the real UI against this server: repeated runs with
   zero request failures and zero page errors.

## Porting gotchas (why some code looks the way it does)

Found the hard way while making the UI run cleanly on this server:

- **Connection headers must be explicit.** Python's `http.server` closes
  `Connection: close` requests silently; Node clients treat a header-less
  HTTP/1.1 response as keep-alive and pool the dying socket → intermittent
  `socket hang up` → 504s in the UI. `send_body` therefore always sends
  `Connection: close|keep-alive` (+ `Keep-Alive: timeout=5`, enforced with a
  socket timeout), matching Node's behavior.
- **Listen backlog**: `http.server` defaults to 5; a UI page load bursts ~20
  parallel connections. `request_queue_size = 511` matches Node.
- **Dual-stack bind**: Node's `listen()` accepts IPv4 and IPv6; Python defaults
  to IPv4-only, and clients resolving `localhost` to `::1` would fail.
- **Chunked request bodies**: axios streams proxied requests with
  `Transfer-Encoding: chunked`, which `BaseHTTPRequestHandler` does not decode.
- **JS semantics in encoders**: `String(3.0) === "3"`, and an empty `$attributes`
  object is truthy in JS (kept on the wire) but falsy in Python.
