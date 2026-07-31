# StoreLink MCP Server (Duvo → Korral pilot)

An MCP server that lets a Duvo agent do a Korral category buyer's job on top of
StoreLink: check stock positions, judge demand, and raise replenishment orders —
with a human approving every order before it reaches StoreLink.

StoreLink calls are stubbed (`src/storelink.py`) behind the same interface a
real HTTPS client would implement; everything above that line is the real
deliverable.

- Full run/deploy options: see [DEPLOYMENT.md](DEPLOYMENT.md)
- Author: Misha Sprindzhuk

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q     # tool shapes, derived math, approval lifecycle
python -m src.main             # MCP over stdio + approvals page on :8765
```

The repo ships a project-scoped [`.mcp.json`](.mcp.json), so Claude Code opened
in this directory discovers the `storelink` server automatically — no manual
config. (Other MCP clients: see the config snippet in
[DEPLOYMENT.md](DEPLOYMENT.md).)

**60-second demo** — the stub data includes a crafted low-stock case:

1. Ask the agent for the stock position of `SKU-0451` at `ST-014`
   (Fjord Smoked Salmon, Antwerp): 12 on hand, ~15/day velocity, 2-day lead
   time → `stockout_risk: true`.
2. Let it raise a replenishment order — the order queues as
   `pending_approval`, nothing hits StoreLink yet.
3. Open <http://127.0.0.1:8765>, read the agent's reason, click **Approve**.
4. Have the agent poll `get_replenishment_order` — status is now `submitted`
   with an expected delivery date.

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
  Korral IT) lives in server config via `StoreKeyProvider`; no tool accepts or
  returns a key. Agents address stores by `store_id` only.
- **SKU search** — agents work from SKU codes the buyer provides; StoreLink has
  no search endpoint and we didn't invent one.
- **Order cancel/edit** — the API doesn't offer it; the human Reject button is
  the kill switch.

**Naming.** Tool names are the buyer's verbs (`get_stock_position`, not
`get_inventory`); docstrings are written as agent-facing contracts, including
when *not* to act (e.g. "do not re-raise a rejected order without new
evidence").

## Structure

```
src/storelink.py     StoreLink client interface + deterministic stub
src/orders.py        Order ledger + approval state machine
src/approvals_ui.py  Human approval web page (stdlib, port 8765)
src/server.py        MCP tool surface (the 5 tools)
src/main.py          Entry point: stdio (default) or --transport http
tests/test_tools.py  Tool shapes, derived math, approval lifecycle
.mcp.json            Project-scoped Claude Code config; auto-connects the server
```

## What's next (out of Step 1 scope)

SQLite persistence for the order ledger (pending approvals must survive a
restart), auth on the approvals page (SSO via reverse proxy), the real HTTPS
StoreLink client with the key-rotation story, and deployment/runbook for
Korral's network.
