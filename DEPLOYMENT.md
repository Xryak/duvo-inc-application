# Deployment

## Prerequisites

- Docker (or Python 3.12+ for local run)
- API keys: copy `.env.example` to `.env` and fill in values

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM calls (Claude) |
| `OPENAI_API_KEY` | LLM calls (optional fallback) |
<!-- TODO: add task-specific vars here -->

## Run with Docker

```bash
docker build -t duvo-task .
docker run --env-file .env duvo-task
```

<!-- TODO: adjust CMD/ports/volumes for the actual task, e.g.:
docker run --env-file .env -p 8000:8000 duvo-task
-->

## Run locally (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.main
```

## Verify it works

<!-- TODO: one command / one URL / expected output so the reviewer can confirm in 30 seconds -->

## Notes & tradeoffs

<!-- TODO: what was cut for the 1-hour scope, what I'd do next -->
