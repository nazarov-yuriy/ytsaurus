# Findings and corrections worth keeping

The commit log for this project was squashed. Routine steps (adding endpoints,
schema fields, docs, deployment plumbing) are not recorded here — the code and
the other docs already describe *what* exists. This file records the things a
reader could not reconstruct: **what we implemented wrongly at first and why the
wrong version looked right**, and **behaviors of YTsaurus, its UI, and the
tooling that surprised us**.

Protocol-level specifics live in `empirical-findings.md`; auth wire details in
`auth.md`; a line-anchored code tour in `../mock-backend-py/REVIEW.md`. This
file is the *why it took several attempts* layer.

---

## 1. Plausible implementations that were wrong

### Invented error codes are indistinguishable from correct ones — until they aren't

The mock originally returned a made-up code `900` for a wrong password. The UI
rendered it fine, screenshots looked perfect, and nothing failed. The real
proxy masks bad credentials as a **generic `TError` code 1, "Incorrect login or
password"** (`cypress_cookie_login.cpp:83`) — deliberately, so clients cannot
distinguish "no such user" from "wrong password". A mock that invents a
distinguishable code teaches the wrong contract to everything built against it.

Same class, later: `//sys/pool_trees/@default_tree`, the scheduler/master
version reads, and the System-page containers all "worked" while returning
errors, because the UI degraded quietly — until a newer UI build did not.

**Rule adopted:** when in doubt, mirror the C++ source and cite it in a comment.
Several deliberate oddities are now preserved on purpose — YT code 500 inside
HTTP 400, `503` for a malformed CSRF token, the `'CSFR'` typo from
`helpers.cpp:187`, string-valued booleans in `web_json`.

### A "convenient" mock feature was a credential leak

Early versions exposed `//sys/cypress_cookies` as ordinary browsable Cypress
data, so anyone could read live session cookies through the anonymous API. It
existed only because it seemed harmless to mirror the real tree.

Related dead code found at the same time: cookie *renewal* was implemented and
could never do anything, because the UI tunnel forwards the cluster-prefixed
cookie captured at login and never propagates a renewed proxy cookie back into
it. Both were deleted.

### "It's only a mock" stopped being true when it became deployable

Published seed credentials (`iceberg`/`iceberg`, `root` with an empty
password), a published robot-token placeholder, a published database password,
CORS echoing any origin *with credentials*, traffic recordings containing
`Authorization` headers, and binding `0.0.0.0` by default were all defensible
while this was a laptop toy. Adding a Helm chart silently turned every one of
them into a deployment default.

The correction was not "remove the defaults" but **fail closed**: the chart now
refuses to render without an explicit authentication posture, refuses published
placeholders, and refuses multi-replica RAM-backed auth; the server refuses to
start with delegated auth behind anonymous fallback, refuses recording under
strict auth, and binds loopback unless told otherwise. Development seeds now
require an explicit opt-in env var that is *ignored* in strict mode.

### Correct-looking sequential code hid two races

- **verify-then-insert**: the login path checked the password, then created a
  session. A concurrent password change in between left a valid session for the
  old password. Fixed by doing it in **one SQL statement** whose `WHERE` clause
  re-checks salt, hash, and `password_revision`.
- **audit-after-response**: audit entries were written after the response was
  produced. A test's `SELECT` legitimately raced the trailing `INSERT`. The
  ordering is now write-then-respond, so the trail can never lag what a client
  already saw. The test caught this, not review.

### Two self-referential bugs in our own tooling

- `sync.py load` used `INSERT OR IGNORE` and then `cur.lastrowid`. For a
  duplicate row the insert is ignored, `lastrowid` keeps its **previous** value,
  and parameters were attached to an unrelated endpoint — surfacing much later
  as a foreign-key error. Guarded with `cur.rowcount`.
- `discover.py` (which finds uncataloged endpoints by diffing recorded traffic
  against the catalog) counted **its own generated stubs** as catalog entries,
  so on the second run it concluded everything was covered and emptied its own
  output. Its input now excludes `discovered.inventory.json`.

Both are the same shape: a tool measuring a system it is also part of.

### Claims that were wrong in the docs, not the code

Review passes corrected several confidently-written statements that had been
inferred rather than verified: the UI server rewrites the proxy's `401` from
`/login` into a `400`; `appAuthPolicy` defaults to *required*; the cluster
version gate is a truthiness check, not a comparison; framing is
`compress(frame(data))`, not the reverse. Each was fixed only after re-reading
the source — a reminder that plausible-sounding protocol prose is the easiest
thing to get wrong and the hardest to notice.

---

## 2. Surprises that cost real debugging time

### Intermittent UI 504s were a transport bug, not a protocol bug

The worst-diagnosed problem of the project. Python's `http.server` closes a
connection for `Connection: close` requests **without saying so**; Node/axios
treats a header-less HTTP/1.1 response as keep-alive and pools the socket, then
fails on reuse with `socket hang up`, which the UI reports as a 504 on a random
unrelated request. Found only by putting a TCP spy between the two processes.

The stdlib layer also needed a raised listen backlog (a page load bursts ~20
connections against a default of 5), dual-stack binding, and manual chunked
request decoding. All four were hand-rolled fixes for things a real ASGI server
does natively — which is what eventually justified moving to FastAPI/uvicorn.
The lesson is not "use a framework": it is that **the mock's hardest bugs were
never in the YT protocol**.

### The published UI image behaves differently from the dev checkout

Three separate failures, all invisible to our headless testing because that
testing ran against the source checkout while deployments use
`ghcr.io/ytsaurus/ui:<tag>`:

1. Newer wrapper builds send command parameters as **base64 numbered header
   parts** (`X-YT-Parameters-0..N`), not one plain JSON header. Unsupported,
   every affected command silently ran with *no parameters*.
2. `support.ts` calls `.match(/(\d+)\.(\d+)\.(\d+)/)` on the scheduler and
   master versions **without an undefined guard**, so a batch error there
   crashes the whole page immediately after cluster selection. Our checkout
   tolerated the same errors.
3. The System page's strictness (below) only manifested there first.

**Rule adopted:** validating against the checkout proves the protocol, not the
deployment. Both need exercising.

### The System page fails harder than any other

It is the strictest surface in the UI, and it masks its own failures: fixing
one error reveals the next.

- `nodes.ts` throws on **any** failed batch item, so a single missing node-type
  map kills the whole section.
- `masters.ts` throws unless *both* primary and secondary master maps resolve,
  and hard-requires the primary master's `cell_id` orchid document.
- `chunks.js` reads `chunks.chunks.count` unguarded — a missing `//sys/chunks`
  crashes the page with a `Cannot read properties of undefined` that looks
  unrelated to anything you just changed.
- `Resources.js` indexes `available_space_per_medium[medium]` without guards
  *once* `//sys/cluster_nodes/@` starts resolving — so fixing one thing armed a
  new crash.

Empty containers and zero counters everywhere; enumerate from the source rather
than iterating on visible errors.

### The UI's authorization check does nothing

`isAuthorized()` is effectively `Boolean(Object.keys(req.yt.ytApiAuthHeaders ?? {}))`,
and `Boolean([])` is `true`. Even `Cookie: YTCypressCookie=undefined` passes it.
**All** real authentication is delegated to the proxy's `/auth/whoami`, which
must return HTTP 200 with a *truthy* `csrf_token` or the entire cluster page
refuses to mount. A mock that gets everything else right and returns a falsy
token looks completely broken with no useful error.

### Determinism is a wire contract

`data.py` assigns node ids sequentially in creation order and uses fixed
timestamps. That is not tidiness: the golden corpus compares responses
byte-for-byte, so **reordering the inserts changes ids and breaks replay**. New
nodes go at the end.

---

## 3. Tooling traps that will recur

- **`pkill -f <pattern>` kills the shell running it** when the compound command
  contains the pattern literally. This bit us at least three times, each costing
  a confusing "exit code 144". Kill by saved PID, or use a bracket pattern from
  a *separate* invocation.
- **A stale server holding the port** produces perfect "my change didn't take"
  illusions. Probe after every restart.
- **uvicorn shuts down gracefully**, so a test teardown that only calls
  `terminate()` without `wait()` lets the next run race the port — which
  presents as mass connection errors, not as a teardown bug.
- **docker semantics cannot be validated by running the container's commands
  directly.** Two bugs shipped this way: compose `command:` does *not* replace
  an image `ENTRYPOINT` (Kubernetes `command:` does, which is why the identical
  line worked in the chart), and the UI image's preflight `chown -R`s
  `/opt/app/secrets`, so a read-only mount inside that directory aborts startup
  before the app runs.

---

## 4. What the process caught that review did not

- The **traffic-discovery loop** (record real UI traffic → diff against the
  catalog) found two commands the UI calls that we had never implemented, and
  then caught its own stub being generated for the wrong API version.
- The **golden replay corpus** made every wire change visible: each regeneration
  in the history was a deliberate decision, and diffing it is how we confirmed
  that changes we believed were additive really were.
- **Adversarial review found real defects on both sides.** An external review
  pass found a genuine login-CSRF vulnerability in the *UI's own* OAuth callback
  (the `state` cookie was set but never verified). Reviewing that reviewer's
  fixes in turn found a real bug in the hardening: comparing published
  credentials with `secrets.compare_digest` on `str` raises on non-ASCII input,
  turning a masked 401 into a 500 that fingerprinted which login names were on
  the published list.

The recurring pattern across all four sections: **the failure was almost never
where the symptom was.**
