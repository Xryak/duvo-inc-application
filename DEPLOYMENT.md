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

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `APPROVALS_HOST` / `APPROVALS_PORT` | `127.0.0.1` / `8765` | Where the approvals page binds |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | HTTP transport bind (only with `--transport http`) |

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

## Notes & tradeoffs

Scoped for the 1-hour session: order ledger is in-memory (needs SQLite before
real deployment), approvals page is unauthenticated (bind internally; add SSO
via reverse proxy for rollout), StoreLink client is a deterministic stub behind
the real client's interface.
