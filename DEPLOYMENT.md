# Deployment

## Prerequisites

- Python 3.12+ (or Docker). No API keys needed — StoreLink is stubbed.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main            # MCP over stdio + approvals page on :8765
```

Hook it into Claude Code / Claude Desktop as a stdio server:

```json
{
  "mcpServers": {
    "storelink": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "src.main"],
      "cwd": "/path/to/this/repo"
    }
  }
}
```

## Run as an in-network service (Docker)

```bash
docker build -t storelink-mcp .
docker run -p 8000:8000 -p 8765:8765 storelink-mcp
```

- MCP endpoint (streamable HTTP): `http://localhost:8000/mcp`
- Human approvals page: `http://localhost:8765`
- Buyer's audit trail: `http://localhost:8765/audit`

Mount a volume at the log directory (`-v storelink-logs:/app/logs`) — the audit
trail is the record of what was ordered and must outlive the container.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `APPROVALS_HOST` / `APPROVALS_PORT` | `127.0.0.1` / `8765` | Where the approvals page binds |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | HTTP transport bind (only with `--transport http`) |
| `STORELINK_LOG_DIR` | `logs` | Where `diagnostic.jsonl` and `audit.jsonl` are written |
| `STORELINK_LOG_MAX_BYTES` | `10485760` | Rotation threshold for the diagnostic log (audit is never rotated) |
| `STORELINK_DIAG_RESULT_BYTES` | `4096` | How much of each tool result to capture; `0` to capture none |
| `STORELINK_DIAG` | `on` | `off` silences the diagnostic stream. The audit stream has no off switch |

## Verify it works (30 seconds)

```bash
python -m pytest tests/ -q        # tool shapes, derived math, approval lifecycle
```

Then the full loop with a connected agent. Demo prompt (crafted data —
store 47 has a real gap, store 102 does not, so the agent should raise
exactly one order):

> SKU 8847291 (Madeta butter 250g) is running empty at stores 47 and 102.
> Check on-hand vs. last 24h of POS for both, and raise a replenishment
> order for any store where the gap exceeds 6 units.

Open `http://127.0.0.1:8765`, click **Approve**, and have the agent
re-check the order — status flips to `submitted` with a delivery date.
(`SKU-0451` at `ST-014` is another crafted stockout: 12 on hand, ~15/day.)

## Debugging a session (Forward Deployed Engineer)

Everything the agent did is in `logs/diagnostic.jsonl`, one JSON object per
line. `logquery` is the shortcut; `grep` and `jq` work on the same file.

```bash
python -m src.logquery sessions                    # who connected, how many calls, how many errors
python -m src.logquery trace --session sess-9a1c   # replay that session, call by call
python -m src.logquery trace --session sess-9a1c --request 7   # one call in detail
python -m src.logquery trace --order RO-1001       # one order across both logs
python -m src.logquery errors --since 1h           # what's failing right now
python -m src.logquery tail -f                     # watch live while the agent runs
python -m src.logquery trace --tool get_stock_position --json | jq .result.value
```

A `tool_call` entry carries the arguments as received, the outcome, the
duration, the result the agent saw, and the StoreLink calls made underneath it
— so "why did it order 480 units?" is answerable from the log alone. Add
`--json` to any command to get raw records for jq.

## Checking what was ordered (buyer)

Open `http://127.0.0.1:8765/audit`: every proposal, decision and StoreLink
submission, newest first, filterable by order / store / SKU / date, with a CSV
download for finance. Each proposal shows the agent's stated reason *and* the
stock numbers it had read. Same data in a terminal:

```bash
python -m src.logquery audit --store ST-047
python -m src.logquery audit --csv > audit.csv
```

## Notes & tradeoffs

Scoped for the 1-hour session: order ledger is in-memory (needs SQLite before
real deployment), approvals page is unauthenticated (bind internally; add SSO
via reverse proxy for rollout), StoreLink client is a deterministic stub behind
the real client's interface.

Logging: both streams are files on the server's disk, written synchronously —
right at pilot volume, and the format is line-delimited JSON so shipping them
to Korral's collector later is a filebeat config, not a rewrite. Because the
approvals page is unauthenticated, an approver's name is self-asserted and every
audit record says so (`actor.verified: false`); SSO flips that without changing
the record shape. Tool arguments are logged verbatim (credential-shaped keys
redacted) and results are captured up to 4 KB — if Korral classes stock data as
sensitive, set `STORELINK_DIAG_RESULT_BYTES=0` and the audit trail is unaffected.
