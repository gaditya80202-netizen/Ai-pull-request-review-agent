# AI Pull-Request Review Agent

An AI-assisted GitHub pull-request reviewer that analyses code changes with four specialist agents: **Security, Code Quality, Tests, and Documentation**. It uses GitHub App webhooks, FastAPI, Redis/ARQ, LangGraph, DeepSeek, Cloudflare Workers AI embeddings, and PostgreSQL/TimescaleDB. A dashboard lets a human inspect findings before publishing a GitHub **COMMENT** review.

## What it does

1. Receives GitHub pull-request and default-branch push events.
2. Queues review and repository-indexing jobs through Redis/ARQ.
3. Indexes repository files and retrieves relevant context using vector and full-text search.
4. Runs four specialist agents and validates/aggregates their findings.
5. Stores reviews, findings, usage, costs, and review state in TimescaleDB.
6. Shows pending reviews and findings in the dashboard. After explicit human approval, it can publish a GitHub COMMENT review with safe inline comments where possible.

**The application does not approve, merge, commit to, or edit the pull request.** Unattended auto-posting is disabled by default.

## Prerequisites

- Python 3.12+
- Node.js/npm (for the Smee webhook relay)
- Podman (or a compatible Redis deployment)
- A PostgreSQL/TimescaleDB database
- A GitHub App installed on the target repository, with repository Contents: Read and Pull requests: Read & write; webhook events for `pull_request` and `push`
- DeepSeek API credentials
- Cloudflare Workers AI account ID and API token for embeddings

## Setup

Extract/clone the project and open a terminal in the project root (the directory containing `requirements.txt`). Create a virtual environment and install dependencies.

### Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

### Linux (Fedora/Ubuntu)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
```

Fill in `.env` with your own GitHub App ID, installation ID, webhook secret, DeepSeek key, Cloudflare credentials, TimescaleDB URL, and a random `HITL_API_TOKEN` of at least 32 characters. Put the matching GitHub App private key at the path specified by `GITHUB_PRIVATE_KEY_PATH` (default: `github-app.pem`). Never commit `.env` or the private key.

The current application defaults used in the demo are `DEEPSEEK_MODEL=deepseek-flash`, `MAX_CONTEXT_TOKENS=131072`, `LLM_MAX_OUTPUT_TOKENS=4096`, and `DAILY_BUDGET_USD=5.0`. The request-size guard is a code constant (`MAX_REQUEST_BYTES = 128 * 1024`) in `backend/integrations/deepseek_client.py`, not an `.env` setting. These are application guardrails, not the provider's full context window.

## Database migrations (one-time)

Apply the SQL files to your TimescaleDB/PostgreSQL database in this order, using `psql` or the provider's SQL console:

1. `scripts/migrations/2026-10-init.sql`
2. `scripts/migrations/2026-10-llm-budget-reservations.sql`
3. `scripts/migrations/2026-10-review-reliability.sql`
4. `scripts/migrations/2026-10-review-publication.sql`

With `psql`, set `TIGER_DATABASE_URL` in your shell to the same connection string used in `.env`, then run the migrations in order.

**PowerShell:**

```powershell
$env:TIGER_DATABASE_URL = "postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require"
psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-init.sql
psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-llm-budget-reservations.sql
psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-reliability.sql
psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-publication.sql
```

**Linux:**

```bash
export TIGER_DATABASE_URL='postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require'
psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-init.sql
psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-llm-budget-reservations.sql
psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-reliability.sql
psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-publication.sql
```

## Start the services

Keep Redis, the API, the worker, and the Smee relay running in separate terminals.

### Redis (Podman)

If the existing `ai-pr-review-redis` container is stopped:

```bash
podman start ai-pr-review-redis
podman exec ai-pr-review-redis redis-cli ping
```

Expected response: `PONG`. For a fresh machine with no container, create a persistent volume and container once:

```bash
podman volume create ai-pr-review-redis-data
podman run -d --name ai-pr-review-redis -p 127.0.0.1:6379:6379 -v ai-pr-review-redis-data:/data redis:8-alpine redis-server --appendonly yes
```

On Windows, start the Podman VM first with `podman machine start`.

### Terminal 1 — FastAPI

```bash
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

### Terminal 2 — ARQ worker

```bash
arq backend.job_queue.arq_worker.WorkerSettings
```

### Terminal 3 — Smee webhook relay

Replace the URL with the Smee channel configured as the GitHub App webhook target:

```bash
npx --yes --package=smee-client smee -u 'YOUR_SMEE_URL' -t 'http://127.0.0.1:8000/webhook/github'
```

The GitHub App webhook URL must be the Smee URL, and the webhook receiver path must target `/webhook/github` on the local API.

## Open the application

- Dashboard: http://127.0.0.1:8000/dashboard
- Health: http://127.0.0.1:8000/health
- API docs: http://127.0.0.1:8000/docs

Enter the configured HITL API token in the dashboard. Review findings, verify the live PR head SHA, and approve or reject the AI review. Approval publishes a GitHub **COMMENT** review; it is not a GitHub APPROVE event and does not merge the PR.

## Tests

```bash
PYTHONPATH="$PWD" python -m pytest -q
```

On PowerShell:

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
```

## Safety notes

- Keep `AUTO_POST_ENABLED=false` and `EVALUATION_GATE_PASSED=false` unless the evaluation policy has been deliberately reviewed and the unattended path is intentionally implemented and enabled.
- Keep `.env`, GitHub `.pem` files, API keys, database credentials, and HITL tokens out of source control and submission archives.
- The dashboard's approval action is a human decision to publish a comment-style review. It does not approve or merge the PR.
- Repository retrieval is bounded; the system is not a formal proof or a guarantee that every defect will be detected.
