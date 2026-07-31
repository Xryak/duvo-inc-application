# Deployment

## Prerequisites

- Python 3.12+ (or Docker). No API keys needed for the demo — StoreLink is
  stubbed and, without `STORELINK_KEYS_FILE` set, dev keys are fabricated for
  every store. For a real deployment, see *Store keys* below.

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

## Store keys (per-store, rotated weekly by Korral IT)

The server loads per-store StoreLink keys from a JSON file:

```json
{"ST-001": "<key>", "ST-002": "<key>"}
```

Point `STORELINK_KEYS_FILE` at it (see `secrets/storelink_keys.example.json`).
Korral IT overwrites this file on each weekly rotation — **atomically, via
write-to-temp + rename** — and the server picks the change up on the next
call, no restart needed. If a key is rejected mid-request (rotation raced the
call), the server reloads the file and retries once automatically. Two
failures Korral IT should know the shape of:

- **Stale file after rotation** → calls for that store fail with a `KeyExpired`
  message naming the store and stating the request was not processed. Fix: land
  the rotated key in the file; in-flight approvals stay pending and can simply
  be re-approved.
- **Store missing from the file** → calls fail immediately with
  `MissingStoreKey`; the store shows `credentialed: false` in `list_stores`.
  Fix: add the key.

The server refuses to start if the file is missing or malformed. Keys never
appear in logs, tool responses, or error messages.

## Run as an in-network service (Docker)

```bash
docker build -t storelink-mcp .
docker run -p 8000:8000 -p 8765:8765 \
  -v /srv/korral/storelink_keys.json:/secrets/storelink_keys.json:ro \
  -e STORELINK_KEYS_FILE=/secrets/storelink_keys.json \
  storelink-mcp
```

(Omit the mount and env var to run the zero-setup demo with dev keys.)

- MCP endpoint (streamable HTTP): `http://localhost:8000/mcp`
- Human approvals page: `http://localhost:8765`

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `APPROVALS_HOST` / `APPROVALS_PORT` | `127.0.0.1` / `8765` | Where the approvals page binds |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | HTTP transport bind (only with `--transport http`) |
| `STORELINK_KEYS_FILE` | *(unset — dev keys)* | Path to the per-store StoreLink keys JSON mounted by Korral IT |

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
