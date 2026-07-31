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

Then the full loop: start the server, ask a connected agent for the stock
position of `SKU-0451` at `ST-014` (crafted low-stock case: 12 on hand,
~15/day velocity → `stockout_risk: true`), let it raise an order, open
`http://127.0.0.1:8765`, click **Approve**, and have the agent re-check the
order — status flips to `submitted` with a delivery date.

## Notes & tradeoffs

Scoped for the 1-hour session: order ledger is in-memory (needs SQLite before
real deployment), approvals page is unauthenticated (bind internally; add SSO
via reverse proxy for rollout), StoreLink client is a deterministic stub behind
the real client's interface.
