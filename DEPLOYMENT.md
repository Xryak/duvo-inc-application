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

Or as a service: `docker build -t storelink-mcp . && docker run -p 8000:8000 -p 8765:8765 storelink-mcp`
→ MCP at `http://localhost:8000/mcp`, approvals at `http://localhost:8765`.
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
  Korral project**, mounted into the container as a JSON file
  (`{"ST-001": "<key>", ...}`) whose path is `STORELINK_KEYS_FILE` (see
  `secrets/storelink_keys.example.json` for the format). Korral IT's weekly
  rotation is just "add a new secret version" — Cloud Run refreshes the
  mounted file, the server stats it on every key lookup and picks the change
  up with **no redeploy, no restart**, and Duvo never handles key material
  out-of-band.
- If StoreLink rejects a key mid-request (rotation raced an in-flight call),
  the server force-reloads the file and retries once automatically. Two
  failure shapes Korral IT will see:
  - **Stale file after rotation** → that store's calls fail with `KeyExpired`,
    naming the store and stating the request was not processed. Pending
    approvals stay pending and can simply be re-approved once the new key
    version lands.
  - **Store missing from the file** → immediate `MissingStoreKey`; the store
    shows `credentialed: false` in `list_stores`. Fix: add the key.
- The server **refuses to start** if the secrets file is missing or malformed;
  a malformed overwrite at runtime keeps the last good keys and logs a
  warning. Keys never appear in logs, tool responses, or error messages.
- Nothing secret is in the image, the repo, or CI. Local dev is zero-config:
  with `STORELINK_KEYS_FILE` unset, fabricated dev keys cover every stub
  store.

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
   the pilot project, and what's the format? (The server currently reads one
   JSON blob of all store keys; one-secret-per-store works with a thin
   adapter in `StoreKeyProvider`.)
5. **Deploy gate + change windows:** auto-deploy for the pilot, or a Korral
   approval click? Any freeze windows (e.g. weekend promos) an 11pm fix must
   respect?
6. **Observability handoff:** who at Korral gets alerted on StoreLink 5xx /
   auth failures, and where do they want to see uptime dashboards?
7. **StoreLink rate limits and staging:** is there a test StoreLink
   environment, and what call volume is acceptable against prod (180 stores ×
   frequent stock checks adds up)?

## Pre-go-live gaps (known, deliberate for the pilot)

- Order ledger → SQLite/Cloud SQL (pending approvals must survive restarts).
- Approvals page consolidated onto the main HTTP port (Cloud Run exposes one
  port) and put behind IAP.
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
