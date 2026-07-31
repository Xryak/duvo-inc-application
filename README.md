# StoreLink MCP Server (Duvo → Korral pilot)

An MCP server that lets a Duvo agent do a Korral category buyer's job on top of
StoreLink: check stock positions, judge demand, and raise replenishment orders —
with a human approving every order before it reaches StoreLink.

StoreLink calls are stubbed (`src/storelink.py`) behind the same interface a
real HTTPS client would implement; everything above that line is the real
deliverable.

- How to run: see [DEPLOYMENT.md](DEPLOYMENT.md)
- Author: Misha Sprindzhuk

## The tool surface

Five tools, curated around the buyer's daily loop — *"how fast is it selling,
will the store be empty before a delivery lands, and if so raise an order"* —
rather than a 1:1 mirror of StoreLink's endpoints:

| Tool | What it answers |
|---|---|
| `list_stores()` | Which stores exist; source of valid `store_id`s |
| `get_stock_position(store_id, sku)` | The core judgment call, in one response: on-hand, 7-day velocity, **days_of_cover**, supplier lead time, **stockout_risk**, and open orders |
| `get_sales_history(store_id, sku, days)` | Demand trend as daily totals — is today a spike or the new normal? |
| `create_replenishment_order(store_id, sku, quantity, reason)` | The only write. Queues the order for **human approval** |
| `get_replenishment_order(order_id)` | Did the human approve it? When does it land? |

## Decisions (and what we deliberately did NOT build)

**Composite reads over raw endpoints.** `get_stock_position` joins four
StoreLink calls (inventory + POS + SKU + supplier) server-side and computes
`days_of_cover = on_hand / avg_daily_units_7d` and
`stockout_risk = days_of_cover < lead_time`. That arithmetic is *the* buyer
judgment; doing it once in tested code beats every agent re-deriving it from
raw dumps per call (token cost + arithmetic errors). The response stays purely
machine-readable — numbers, no narrative "advice" fields — so judgment remains
with the agent and the approving human.

**Human-in-the-loop on the only write.** `create_replenishment_order` never
hits StoreLink directly. Orders queue as `pending_approval`; a buyer approves
or rejects them on a minimal web page served by the same process
(`http://<host>:8765`), and only approval triggers the StoreLink POST. The
gate is async — the tool returns immediately and the agent polls
`get_replenishment_order` — because blocking a tool call on a human who may be
at lunch is fragile. (A console prompt is impossible anyway: over stdio,
stdin/stdout carry the MCP protocol.)

**Required `reason` on every order.** Shown to the approver and kept in the
order record — every unit of spend traces back to stated evidence.

**An order ledger the server owns.** StoreLink has no list-orders endpoint
(only get-by-id), so the server records every order it raises and surfaces
open ones inside `get_stock_position` — the guard against ordering twice for
the same gap. Raising a duplicate is allowed but returns a warning naming the
existing order. *Known limitation:* orders raised outside this server are
invisible to it.

**Not exposed, on purpose:**
- **Raw POS transactions** — agents get daily aggregates only; receipt-level
  dumps burn context and add nothing to the decision.
- **Supplier endpoint** — lead time is folded into `get_stock_position`.
- **Store keys / auth** — the per-store `X-Korral-Store-Key` (rotated weekly by
  Korral IT) lives in a mounted secrets file read via `StoreKeyProvider`; no
  tool accepts or returns a key, and no error message ever contains one.
  Agents address stores by `store_id` only. See *Secrets & key rotation* below.
- **SKU search** — agents work from SKU codes the buyer provides; StoreLink has
  no search endpoint and we didn't invent one.
- **Order cancel/edit** — the API doesn't offer it; the human Reject button is
  the kill switch.

**Two log streams, not one.** Debugging and accountability are different jobs
with different readers, so they get different files. Merging them would give
the buyer MCP internals and give the engineer a file they must not truncate.

| | `logs/diagnostic.jsonl` (FDE) | `logs/audit.jsonl` (buyer) |
|---|---|---|
| Answers | "what did the agent call, with what arguments, in what context, and what came back?" | "what was ordered on my behalf, on what evidence, and who signed off?" |
| Written per | MCP message — every `tools/call`, plus each upstream StoreLink call and every error | business event — proposal, approval, rejection, StoreLink submission |
| Keyed by | `session_id` + `request_id` | `order_id`, with a `trace` back to the request that caused it |
| Read via | `python -m src.logquery` | the **Audit trail** page at `/audit`, or CSV |
| Retention | rotates at 10 MB, safe to delete, can be switched off | append-only, never rotated, cannot be switched off |

The correlation ids are the point: `logquery trace --session <id>` replays a
whole agent conversation; `--request <id>` narrows to one call; `trace --order
RO-1001` follows a single order across *both* streams — from the tool call that
raised it to the buyer's click that approved it.

Two things the diagnostic stream records that a plain "log the tool call"
wouldn't: the **upstream StoreLink calls** each tool made (a wrong
`days_of_cover` is usually a wrong input, and this shows all four inputs), and
the **result the agent actually saw** (bounded to 4 KB), because explaining
agent behaviour means knowing what it was told. Arguments are logged verbatim
with credential-shaped keys redacted.

**Evidence, not just reasons, in the audit trail.** `reason` is the agent's
claim; every proposal entry also carries the stock numbers the agent had read
for that store+SKU before proposing — on hand, velocity, days of cover,
lead time, risk flag. A buyer checking last week's orders can see whether the
stated reason matched the data. Decisions record the approver's name from the
approvals page, marked `verified: false` while that page is unauthenticated —
the record stays honest about how much it knows.

**Naming.** Tool names are the buyer's verbs (`get_stock_position`, not
`get_inventory`); docstrings are written as agent-facing contracts, including
when *not* to act (e.g. "do not re-raise a rejected order without new
evidence").

## Secrets & key rotation

StoreLink keys are per-store and rotated weekly by Korral IT. The server reads
them from a JSON file (`{"ST-001": "<key>", ...}`) pointed to by
`STORELINK_KEYS_FILE` — the file IT overwrites on rotation. The file is
stat'ed on every lookup and re-read when it changes, so rotations land
**without a restart**; without the env var set, fabricated dev keys keep the
stubbed demo runnable with zero setup.

Every store-scoped StoreLink call goes through one wrapper (`_store_call`),
which owns the two failure stories:

**(a) Key rotates while a request is in flight.** The call goes out with the
cached key, StoreLink rejects it, the wrapper force-reloads the secrets file
and retries **exactly once** with the fresh key — normally invisible to the
agent. The retry is safe even for the order write: a call rejected for auth
was never processed. If the reloaded key is *still* rejected (the rotation
hasn't reached this server's file yet), the call fails as `KeyExpired` with a
message stating the store, that the key has expired, that the request was not
processed, and who to call (Korral IT). If this happens while a human clicks
Approve, the order simply stays `pending_approval` — nothing is lost, and
Approve works again once the key lands.

**(b) Agent asks for a store we hold no credential for.** Fails up front as
`MissingStoreKey` — before any StoreLink traffic and before any order is
created — naming the store and pointing at Korral IT. `list_stores` exposes a
`credentialed` flag per store so the agent can see its actual reach instead of
discovering it by failing.

Operational edges: the server **refuses to start** if the secrets file is
missing or malformed (better than running credential-less), but a malformed
*overwrite* at runtime (e.g. read mid-write) keeps the last good keys and
logs a warning rather than taking every store down.

Key events land in the diagnostic stream too, correlated to the request that
hit them: `storelink_keys_loaded` (with the store ids whose credential
changed — never the values), `storelink_key_rotation_retry`,
`storelink_key_expired`, `storelink_keys_reload_failed`. A retry is invisible
to the agent by design, so "did Tuesday's rotation cost us anything?" has to
be answerable afterwards: `python -m src.logquery trace --grep key`. No key
material is ever written to either log or into any error message.

## Structure

```
src/storelink.py     StoreLink client interface + deterministic stub
src/orders.py        Order ledger + approval state machine
src/approvals_ui.py  Human pages (stdlib, port 8765): approvals + /audit trail
src/server.py        MCP tool surface (the 5 tools)
src/main.py          Entry point: stdio (default) or --transport http
src/eventlog.py      Append-only JSONL sinks shared by both log streams
src/diagnostics.py   FDE stream: tool-call middleware + upstream call capture
src/audit.py         Buyer stream: order proposals, decisions, submissions
src/logquery.py      `python -m src.logquery` — sessions / trace / errors / audit
tests/test_tools.py  Tool shapes, derived math, approval lifecycle
tests/test_keys.py   Secret loading, rotation retry, missing-credential paths
tests/test_logging.py  Both streams, correlation, redaction, the query CLI
```

## What's next (out of Step 1 scope)

SQLite persistence for the order ledger (pending approvals must survive a
restart), auth on the approvals page (SSO via reverse proxy — which is also
what turns `actor.verified` true in the audit trail), the real HTTPS
StoreLink client (the key-loading/rotation layer it will sit on is done), shipping `diagnostic.jsonl` to
Korral's log collector with a retention policy, and deployment/runbook for
Korral's network.
