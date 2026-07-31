# Deployment

**TL;DR:** the server runs as a container **inside Korral's GCP tenancy** (a
dedicated project, internal-only Cloud Run), because StoreLink is unreachable
from the internet and POS/inventory data may not leave the tenancy. Duvo owns
the build pipeline; Korral owns the infrastructure and the prod gate. The
runnable artifact is the [`Dockerfile`](Dockerfile) in this repo.

## Local run (dev / review)

```bash
pip install -r requirements.txt
python -m src.main                    # MCP over stdio + approvals page on :8765
python -m pytest tests/ -q            # verify: tool shapes, math, approval flow
```

Or as a service: `docker build -t storelink-mcp . && docker run -p 8000:8000 -p 8765:8765 -v storelink-logs:/app/logs storelink-mcp`
→ MCP at `http://localhost:8000/mcp`, approvals at `http://localhost:8765`,
audit trail at `http://localhost:8765/audit`. The volume matters: it holds the
audit trail, which must outlive the container.
A stdio config for Claude Code lives in [`.mcp.json`](.mcp.json). Demo prompt
and crafted scenarios: see "Verify" at the bottom.

## Where it runs

- A **Korral-owned GCP project** in their tenancy (e.g. `korral-duvo-pilot`),
  so nothing about data residency is up for debate: StoreLink calls, POS data,
  logs, and the order ledger never cross the tenancy boundary.
- **Cloud Run, ingress internal-only**, egress via direct VPC egress into the
  network where StoreLink lives. No public IP anywhere. The Duvo agent must
  therefore also run inside the tenancy (or reach the endpoint via IAP) — see
  the day-1 questions.
- Logs go to Cloud Logging **in that project**, not to Duvo.
- Fallback if Korral prefers: the same container on a small GCE VM or their
  GKE cluster — the artifact doesn't change, only the runbook does.

## How it gets there

1. Duvo CI (GitHub Actions) builds the image on every tagged commit, runs the
   test suite, and pushes to **Artifact Registry in the Korral project** using
   Workload Identity Federation — a service account scoped to exactly two
   things: push to that registry, deploy that one Cloud Run service. No
   long-lived keys in CI.
2. CI deploys to a **staging** Cloud Run service and runs a smoke check
   (list tools, read a stock position, raise + reject an order).
3. Promotion to prod is the same image digest — no rebuild — behind whatever
   gate Korral chooses (auto for the pilot, click-to-approve later).

Code flows *in*; data never flows *out*. That asymmetry is the whole design.

## Secrets

- The per-store `X-Korral-Store-Key`s live in **GCP Secret Manager in the
  Korral project**. The server reads them at startup and re-reads
  periodically, so Korral IT's weekly rotation is just "add a new secret
  version" — no redeploy, no Duvo involvement, and Duvo never handles key
  material out-of-band.
- Nothing secret is in the image, the repo, or CI. The stub needs no secrets
  at all, which is why local dev is zero-config.

## Who owns what

| Thing | Owner |
|---|---|
| Source, CI, image build, staging deploys | **Duvo** |
| GCP project, network, IAM, VPC access to StoreLink | **Korral** |
| Store keys + rotation (Secret Manager) | **Korral** |
| Prod deploy gate (and the right to freeze it) | **Korral** |
| On-call for the MCP server | **Duvo**, with a named Korral IT contact |

## Shipping a fix at 11pm

1. Commit → tag → CI builds, tests, pushes, deploys to staging (~5 min).
2. Smoke passes → promote the same digest to prod. Cloud Run revisions make
   this atomic; in-flight requests drain.
3. **Rollback is one command** — route traffic back to the previous revision
   (`gcloud run services update-traffic ... --to-revisions=PREV=100`).
   Rollback first, diagnose second.
4. **Break-glass** (CI itself is down): Duvo on-call holds the deployer role
   directly and runs the documented `gcloud run deploy` with the last known
   good digest from Artifact Registry. Every break-glass use gets written up
   for Korral IT next morning.
- Blast-radius note: the pending-approval queue is in-memory today, so a
  redeploy drops pending orders (they're simply re-raised). Making the ledger
  SQLite/Cloud SQL-backed is on the pre-go-live list below.

## Logs: debugging (Duvo) and the audit trail (Korral)

Two streams, two readers, two retention rules — see the README for why they
are separate files. Both are line-delimited JSON under `STORELINK_LOG_DIR`.

**Debugging a session (Forward Deployed Engineer)** — everything the agent did
is in `diagnostic.jsonl`. `logquery` is the shortcut; `grep` and `jq` work on
the same file.

```bash
python -m src.logquery sessions                    # who connected, how many calls, how many errors
python -m src.logquery trace --session sess-9a1c   # replay that session, call by call
python -m src.logquery trace --session sess-9a1c --request 7   # one call in detail
python -m src.logquery trace --order RO-1001       # one order across both streams
python -m src.logquery errors --since 1h           # what's failing right now
python -m src.logquery tail -f                     # watch live while the agent runs
python -m src.logquery trace --tool get_stock_position --json | jq .result.value
```

A `tool_call` entry carries the arguments as received, the outcome, the
duration, the result the agent saw, and the StoreLink calls made underneath it
— so "why did it order 480 units?" is answerable from the log alone.

**Checking what was ordered (buyer)** — `http://<approvals-host>/audit`: every
proposal, decision and StoreLink submission, newest first, filterable by order
/ store / SKU / date, with a CSV download for finance. Each proposal shows the
agent's stated reason *and* the stock numbers the agent had read. Same data in
a terminal: `python -m src.logquery audit --store ST-047 [--csv]`.

| Variable | Default | Purpose |
|---|---|---|
| `STORELINK_LOG_DIR` | `logs` | Where `diagnostic.jsonl` and `audit.jsonl` are written |
| `STORELINK_LOG_MAX_BYTES` | `10485760` | Rotation threshold for the diagnostic log (audit is never rotated) |
| `STORELINK_DIAG_RESULT_BYTES` | `4096` | How much of each tool result to capture; `0` to capture none |
| `STORELINK_DIAG` | `on` | `off` silences the diagnostic stream. The audit stream has no off switch |

**In the tenancy:** both streams stay inside Korral's project, like everything
else. Cloud Run's filesystem is ephemeral, so `audit.jsonl` needs a durable
home before go-live (GCS bucket via mount, or the same database the order
ledger moves to) — a revision swap must not lose the record of what was
ordered. `diagnostic.jsonl` is disposable and can just be tailed into Cloud
Logging. Tool arguments are logged verbatim (credential-shaped keys redacted)
and results captured up to 4 KB; if Korral classes stock data as sensitive,
set `STORELINK_DIAG_RESULT_BYTES=0` — the audit trail is unaffected.

## To confirm with Korral IT before day 1

1. **Network path to StoreLink:** URL, port, TLS, firewall rules, DNS from
   the pilot project's VPC. Can we get a smoke-test window with one real
   store key?
2. **Where does the agent run — and where do LLM calls go?** "No customer
   data leaves the tenancy" must be squared with the fact that an agent sends
   stock/POS numbers to an LLM API. Does Korral's policy treat that as
   customer data? If yes: Claude on Vertex AI inside their tenancy is the
   answer, and it changes nothing in this repo but everything in the
   agent's deployment.
3. **Approvals page access & auth:** which buyers get it, and is IAP in front
   of it acceptable? (It ships unauthenticated on an internal port today —
   fine behind IAP, not fine otherwise.)
4. **Secret Manager handoff:** can IT's rotation job write secret versions to
   the pilot project, and what's the format (one secret per store vs. one
   JSON blob)?
5. **Deploy gate + change windows:** auto-deploy for the pilot, or a Korral
   approval click? Any freeze windows (e.g. weekend promos) an 11pm fix must
   respect?
6. **Observability handoff:** who at Korral gets alerted on StoreLink 5xx /
   auth failures, and where do they want to see uptime dashboards? Also: how
   long should the diagnostic stream be retained, and who besides the buying
   team may read the audit trail (it names who approved what)?
7. **StoreLink rate limits and staging:** is there a test StoreLink
   environment, and what call volume is acceptable against prod (180 stores ×
   frequent stock checks adds up)?

## Pre-go-live gaps (known, deliberate for the pilot)

- Order ledger → SQLite/Cloud SQL (pending approvals must survive restarts).
- `audit.jsonl` → durable storage alongside it (GCS or the same database).
  Cloud Run's disk is ephemeral; the audit trail must outlive a revision.
- Approvals page **and the `/audit` trail** consolidated onto the main HTTP
  port (Cloud Run exposes one port) and put behind IAP. IAP is also what turns
  an approver's self-asserted name into an authenticated one
  (`actor.verified: true`), without changing the audit record's shape.
- Real StoreLink HTTP client swapped in behind the existing interface, with
  retry/backoff and a health endpoint that checks StoreLink reachability.

## Verify (stub scenarios)

Demo prompt for a connected agent — store 47 has a real gap, store 102 does
not, so a correct agent raises exactly one order:

> SKU 8847291 (Madeta butter 250g) is running empty at stores 47 and 102.
> Check on-hand vs. last 24h of POS for both, and raise a replenishment
> order for any store where the gap exceeds 6 units.

Then open `http://127.0.0.1:8765`, click **Approve**, and have the agent
re-check the order — status flips to `submitted` with a delivery date.
(`SKU-0451` at `ST-014` is another crafted stockout: 12 on hand, ~15/day.)
