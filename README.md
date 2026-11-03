# 🤖 AI Pull-Request Review Agent

<p align="center">
  <strong>An AI-powered code review system for GitHub Pull Requests</strong><br>
  Analyze code changes, detect potential issues, and keep a human in control of every published review.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/LangGraph-Orchestration-1C3C3C" alt="LangGraph">
  <img src="https://img.shields.io/badge/LLM-DeepSeek-4D6BFE" alt="DeepSeek">
  <img src="https://img.shields.io/badge/Redis-Job_Queue-DC382D?logo=redis&logoColor=white" alt="Redis">
  <img src="https://img.shields.io/badge/PostgreSQL-Database-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL">
</p>

<p align="center">
  <strong>Security • Code Quality • Tests • Documentation</strong>
</p>

---

## 📌 Overview

The **AI Pull-Request Review Agent** is an AI-assisted GitHub pull-request reviewer that analyzes code changes using four specialist agents:

- 🔐 **Security Agent** — Identifies potential security vulnerabilities.
- 🧹 **Code Quality Agent** — Examines code quality and maintainability.
- 🧪 **Testing Agent** — Evaluates testing-related issues and gaps.
- 📚 **Documentation Agent** — Checks documentation-related issues.

The system combines GitHub App webhooks, FastAPI, Redis/ARQ, LangGraph, DeepSeek, Cloudflare Workers AI embeddings, and PostgreSQL/TimescaleDB.

A human-in-the-loop dashboard allows an operator to inspect findings before publishing a GitHub **COMMENT** review.

> [!IMPORTANT]
> **Human approval is required.** The application does not approve, merge, commit to, or edit pull requests. Unattended auto-posting is disabled by default.

## ⚙️ How It Works

```mermaid
flowchart TD
    A[GitHub Pull Request / Push Event] --> B[Smee Webhook Relay]
    B --> C[FastAPI Webhook Receiver]
    C --> D[Signature Validation & Durable Inbox]
    D --> E[Redis / ARQ Job Queue]
    E --> F[Review Worker]
    F --> G[Repository Context Retrieval]
    G --> H[LangGraph Review Workflow]

    H --> I[Security Agent]
    H --> J[Code Quality Agent]
    H --> K[Testing Agent]
    H --> L[Documentation Agent]

    I --> M[Finding Validation & Aggregation]
    J --> M
    K --> M
    L --> M

    M --> N[TimescaleDB / PostgreSQL]
    N --> O[Human Review Dashboard]
    O --> P{Human Decision}
    P -->|Approve publication| Q[GitHub COMMENT Review]
    P -->|Reject| R[No Review Published]
```

### 🔄 Review Pipeline

1. Receives GitHub pull-request and default-branch push events.
2. Queues review and repository-indexing jobs through Redis/ARQ.
3. Indexes repository files and retrieves relevant context using vector and full-text search.
4. Runs four specialist agents and validates and aggregates their findings.
5. Stores reviews, findings, usage, costs, and review state in TimescaleDB.
6. Displays pending reviews and findings in the dashboard.
7. After explicit human approval, publishes a GitHub COMMENT review with safe inline comments where possible.

## ✨ Key Features

| Feature | Description |
|---|---|
| 🤖 Multi-agent review | Four specialist agents analyze different aspects of a PR. |
| 🧠 Repository context | Retrieves relevant code context using embeddings and full-text search. |
| 🔗 GitHub integration | Receives pull-request and push events through a GitHub App. |
| ⚡ Asynchronous processing | Redis and ARQ handle background review and indexing jobs. |
| 🧩 Workflow orchestration | LangGraph coordinates specialist agents and aggregation. |
| 📊 Cost tracking | Stores model usage, token counts, latency, and available cost data. |
| 🛡️ Human-in-the-loop | A dashboard allows review inspection and explicit publication approval. |
| 💬 Safe publication | Publishes a GitHub COMMENT review rather than approving or merging a PR. |

## 🧰 Technology Stack

| Component | Technology |
|---|---|
| Main language | Python |
| API | FastAPI |
| Agent orchestration | LangGraph |
| LLM provider | DeepSeek |
| Embeddings | Cloudflare Workers AI |
| Database | PostgreSQL / TimescaleDB |
| Background queue | Redis + ARQ |
| GitHub integration | GitHub App API |
| Webhook relay | Smee |
| Container runtime | Podman |
| Testing | Pytest |

---

## 🚀 Getting Started

### Prerequisites

Before setting up the project, ensure you have:

- Python 3.12+
- Node.js/npm (for the Smee webhook relay)
- Podman (or a compatible Redis deployment)
- A PostgreSQL/TimescaleDB database
- A GitHub App installed on the target repository, with:
  - Repository Contents: Read
  - Pull requests: Read & write
  - Webhook events: `pull_request` and `push`
- DeepSeek API credentials
- Cloudflare Workers AI account ID and API token for embeddings

### 1. Clone the Repository

```bash
git clone https://github.com/gaditya80202-netizen/Ai-pull-request-review-agent.git
cd Ai-pull-request-review-agent
```

### 2. Create the Python Environment

Extract/clone the project and open a terminal in the project root (the directory containing `requirements.txt`).

**Windows — PowerShell**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

**Linux — Fedora / Ubuntu**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
```

### 3. Configure Environment Variables

Fill in `.env` with your own:

- GitHub App ID
- GitHub installation ID
- GitHub webhook secret
- DeepSeek API key
- Cloudflare credentials
- TimescaleDB connection URL
- A random `HITL_API_TOKEN` of at least 32 characters

Put the matching GitHub App private key at the path specified by `GITHUB_PRIVATE_KEY_PATH` (default: `github-app.pem`).

**Never commit `.env` or the private key.**

#### Application Defaults

| Setting | Default |
|---|---|
| `DEEPSEEK_MODEL` | `deepseek-flash` |
| `MAX_CONTEXT_TOKENS` | `131072` |
| `LLM_MAX_OUTPUT_TOKENS` | `4096` |
| `DAILY_BUDGET_USD` | `5.0` |

The request-size guard is a code constant (`MAX_REQUEST_BYTES = 128 * 1024`) in `backend/integrations/deepseek_client.py`, not an `.env` setting.

These are application guardrails, not the provider's full context window.

---

## 🗄️ Database Migrations

Apply the SQL files to your TimescaleDB/PostgreSQL database in this order, using `psql` or the provider's SQL console:

1. `scripts/migrations/2026-10-init.sql`
2. `scripts/migrations/2026-10-llm-budget-reservations.sql`
3. `scripts/migrations/2026-10-review-reliability.sql`
4. `scripts/migrations/2026-10-review-publication.sql`

With `psql`, set `TIGER_DATABASE_URL` in your shell to the same connection string used in `.env`, then run the migrations in order.

**Windows — PowerShell**

```powershell
$env:TIGER_DATABASE_URL = "postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require"

psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-init.sql
psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-llm-budget-reservations.sql
psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-reliability.sql
psql $env:TIGER_DATABASE_URL -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-publication.sql
```

**Linux — Fedora / Ubuntu**

```bash
export TIGER_DATABASE_URL='postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require'

psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-init.sql
psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-llm-budget-reservations.sql
psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-reliability.sql
psql "$TIGER_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/2026-10-review-publication.sql
```

---

## ▶️ Start the Services

Keep Redis, the API, the worker, and the Smee relay running in separate terminals.

### 1. Redis — Podman

If the existing `ai-pr-review-redis` container is stopped:

```bash
podman start ai-pr-review-redis
podman exec ai-pr-review-redis redis-cli ping
```

Expected response:

```text
PONG
```

For a fresh machine with no container, create a persistent volume and container once:

```bash
podman volume create ai-pr-review-redis-data

podman run -d \
  --name ai-pr-review-redis \
  -p 127.0.0.1:6379:6379 \
  -v ai-pr-review-redis-data:/data \
  redis:8-alpine \
  redis-server --appendonly yes
```

On Windows, start the Podman VM first:

```bash
podman machine start
```

### 2. Terminal 1 — FastAPI

```bash
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

### 3. Terminal 2 — ARQ Worker

```bash
arq backend.job_queue.arq_worker.WorkerSettings
```

### 4. Terminal 3 — Smee Webhook Relay

Replace the URL with the Smee channel configured as the GitHub App webhook target:

```bash
npx --yes --package=smee-client smee \
  -u 'YOUR_SMEE_URL' \
  -t 'http://127.0.0.1:8000/webhook/github'
```

The GitHub App webhook URL must be the Smee URL, and the webhook receiver path must target `/webhook/github` on the local API.

---

## 🌐 Open the Application

Once all services are running:

| Service | URL |
|---|---|
| 🖥️ Dashboard | [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard) |
| ❤️ Health Check | [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) |
| 📖 API Documentation | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) |

Enter the configured HITL API token in the dashboard.

Review the findings, verify the live PR head SHA, and approve or reject the AI review.

Approval publishes a GitHub **COMMENT** review. It is not a GitHub APPROVE event and does not merge the PR.

---

## 🧪 Running Tests

Run the test suite from the project root.

**Linux / Fedora / Ubuntu**

```bash
PYTHONPATH="$PWD" python -m pytest -q
```

**Windows — PowerShell**

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
```

---

## 🔐 Safety Notes

- Keep `AUTO_POST_ENABLED=false` and `EVALUATION_GATE_PASSED=false` unless the evaluation policy has been deliberately reviewed and the unattended path is intentionally implemented and enabled.
- Keep `.env`, GitHub `.pem` files, API keys, database credentials, and HITL tokens out of source control and submission archives.
- The dashboard's approval action is a human decision to publish a comment-style review.
- The application does not approve or merge pull requests.
- Repository retrieval is bounded; the system is not a formal proof or a guarantee that every defect will be detected.

---

## 📁 Project Repository

**GitHub:** [gaditya80202-netizen/Ai-pull-request-review-agent](https://github.com/gaditya80202-netizen/Ai-pull-request-review-agent)

---

<p align="center">
  Built with Python, LangGraph, DeepSeek, and a focus on safer AI-assisted code reviews.
</p>
