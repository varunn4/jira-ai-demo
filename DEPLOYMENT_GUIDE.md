# AI Governor — Deployment Guide

Universal setup and configuration guide for deploying the AI Governor (JIRA-AI) platform into any client organization.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Codebase Structure](#3-codebase-structure)
4. [Environment Configuration](#4-environment-configuration)
5. [Docker Deployment](#5-docker-deployment)
6. [Jira Integration](#6-jira-integration)
7. [Slack Integration](#7-slack-integration)
8. [n8n Workflow Setup](#8-n8n-workflow-setup)
9. [Database Setup](#9-database-setup)
10. [Vector Search & Embeddings](#10-vector-search--embeddings)
11. [Neo4j Code Graph](#11-neo4j-code-graph)
12. [Frontend Build](#12-frontend-build)
13. [RBAC & User Management](#13-rbac--user-management)
14. [RepoTree / Repomix Setup](#14-repotree--repomix-setup)
15. [Optional Integrations](#15-optional-integrations)
16. [API Endpoints Reference](#16-api-endpoints-reference)
17. [n8n Workflow Reference](#17-n8n-workflow-reference)
18. [Database Schema Reference](#18-database-schema-reference)
19. [LLM Prompts](#19-llm-prompts)
20. [Troubleshooting](#20-troubleshooting)

---

## 1. Architecture Overview

```
┌──────────────┐    Webhook     ┌──────────────┐   HTTP POST    ┌──────────────────┐
│    Jira       │──────────────▶│    n8n        │──────────────▶│  FastAPI (api.py) │
│  (Cloud)      │               │  (Orchestr.)  │◀──────────────│  Port 8000        │
└──────────────┘               └──────────────┘   JSON resp.   └────────┬─────────┘
                                      │                                  │
                                      ▼                                  ▼
                               ┌──────────────┐               ┌──────────────────┐
                               │    Slack      │               │   Claude / LLM    │
                               │  (Bot DMs)    │               │   (Anthropic API) │
                               └──────────────┘               └──────────────────┘
                                                                        │
                                      ┌─────────────────────────────────┤
                                      ▼                                 ▼
                               ┌──────────────┐               ┌──────────────────┐
                               │  PostgreSQL   │               │  Qdrant (Vector)  │
                               │  (Main DB)    │               │  + Ollama (Embed) │
                               └──────────────┘               └──────────────────┘
```

**Design principle:** n8n handles orchestration (triggers, fan-out, Slack posting). FastAPI handles all intelligence (LLM calls, retrieval, DB operations). No AI logic lives in n8n.

**Tech stack:**

- **Backend:** Python 3.12, FastAPI, uvicorn
- **LLM:** Anthropic Claude (Opus, Sonnet), OpenAI (fallback)
- **Orchestration:** n8n (self-hosted, Docker)
- **Database:** PostgreSQL 16
- **Vector Search:** Qdrant + Ollama (bge-m3) + FlagEmbedding
- **Graph DB:** Neo4j (code knowledge graph)
- **Frontend:** React 18, Vite 5, react-router-dom 6
- **Deployment:** Docker on any Linux server, behind Caddy reverse proxy
- **External APIs:** Jira Cloud, Slack Bot, GitHub, Google Docs, Zoho Desk

---

## 2. Prerequisites

**Server requirements:**

- Linux server (Ubuntu 22.04+ recommended), 4+ vCPUs, 16+ GB RAM
- Docker Engine 24+ with Compose plugin (`docker compose`)
- Git, SSH key for GitHub access
- Ports: 8000 (API), 5678 (n8n), 5432 (Postgres), 6333 (Qdrant), 11434 (Ollama), 7474/7687 (Neo4j)

**External accounts:**

- Anthropic API key (required)
- Jira Cloud instance with API token
- Slack workspace with a Bot app (`xoxb-` token)
- GitHub PAT or SSH key for repository access
- OpenAI API key (optional fallback)

**Install Docker Compose plugin if missing:**

```bash
sudo apt install docker-compose-v2
```

---

## 3. Codebase Structure

```
JIRA-AI/
├── api.py                          # Main FastAPI app (~95 endpoints)
├── main.py                         # Local CLI runner
├── requirements.txt                # Python dependencies
├── Dockerfile                      # Python 3.12-slim + Node 22 + repomix
├── docker-compose.yml              # jira-ai-api service + optional postgres
├── .env                            # Environment variables (not in git)
│
├── app/                            # Core application code
│   ├── config.py                   # Settings dataclass (120+ env vars)
│   ├── auth.py                     # JWT auth, RBAC, user CRUD
│   ├── llm_client.py               # LLM provider abstraction
│   ├── workflow1_reviewer.py       # WF1: Ticket validation
│   ├── workflow2_replier.py        # WF2: Slack thread replies
│   ├── testcase_chat_workflow.py   # WF2/5/5b: Test case Q&A + edit
│   ├── workflow3_sla.py            # WF3: SLA monitoring
│   ├── workflow4_due_date.py       # WF4: Due-date compliance
│   ├── jira_client.py              # Jira REST API client
│   ├── slack_client.py             # Slack Bot API client
│   ├── neo4j_graph/                # Neo4j code graph subsystem
│   ├── rca/                        # Root Cause Analysis agentic subsystem
│   └── ...                         # ~50 modules total
│
├── repo_architect/                 # RepoTree/Repomix code analysis subsystem
├── frontend/                       # React Admin SPA (Vite + React 18)
├── Prompt/                         # LLM prompt templates
├── N8N flows/                      # n8n workflow JSON exports (8 workflows)
├── scripts/                        # Utility scripts (admin, oauth, SQL)
├── repo_tree/                      # RepoTree workspace & config
│   ├── config/repos.yaml           # Repository list (edit per deployment)
│   └── workspace/                  # Generated artifacts (gitignored)
└── tests/                          # Unit tests
```

---

## 4. Environment Configuration

Create a `.env` file in the project root. All variables are read by the `Settings` dataclass in `app/config.py`.

### 4.1 Core (Required)

```env
DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/jira_ai
JWT_SECRET=<random-64-char-secret>
JWT_EXPIRE_MINUTES=720
ADMIN_EMAIL=admin@yourcompany.com
ADMIN_PASSWORD=<strong-initial-password>
SERVICE_API_KEY=<optional-key-for-server-to-server-auth>
```

### 4.2 LLM (Required)

```env
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-20250514
LLM_PROVIDER=anthropic
LLM_TIMEOUT_SECONDS=120
TESTCASE_CHAT_MODEL=claude-sonnet-4-20250514

# Optional fallback
OPENAI_API_KEY=sk-...
```

### 4.3 Jira

```env
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=jira-bot@yourcompany.com
JIRA_API_TOKEN=<jira-api-token>
JIRA_PROJECT_KEYS=PROJ1,PROJ2
JIRA_EXCLUDED_PROJECT_KEYS=AIGOV
JIRA_APPROVED_TRANSITION_NAME=LLM APPROVED

# Custom field IDs (find via Jira REST API /rest/api/2/field)
JIRA_DEV_DUE_DATE_FIELD=customfield_10100
JIRA_QA_DUE_DATE_FIELD=customfield_10101
JIRA_LIVE_DUE_DATE_FIELD=customfield_10102
```

`JIRA_PROJECT_KEYS` is optional when the API user has Browse Projects permission globally. Set it to restrict monitoring to specific projects.

### 4.4 Slack

```env
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_DEFAULT_CHANNEL_ID=C1234567890
GOVERNOR_NOTIFY_CHANNEL_ID=C1234567890
```

### 4.5 Vector & Embeddings

```env
QDRANT_URL=http://localhost:6333
OLLAMA_URL=http://localhost:11434
OLLAMA_EMBED_MODEL=bge-m3
SIMILAR_TICKET_MATCH_THRESHOLD=0.68
```

### 4.6 Neo4j

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<neo4j-password>
```

### 4.7 n8n

```env
N8N_BASE_URL=https://your-domain.com/n8n
N8N_API_KEY=<n8n-api-key>
N8N_GRAPH_WEBHOOK_URL=http://localhost:5678/webhook/graph-db-admin
```

### 4.8 GitHub

```env
GITHUB_TOKEN=ghp_...
GITHUB_ORG=your-org-name
```

### 4.9 Repository Discovery

```env
REPOSITORY_SEARCH_ROOT=/home/ubuntu
REPOSITORY_HOST_ROOT=/home/ubuntu
EXCLUDED_REPOSITORY_NAMES=JIRA-AI,other-excluded-repo
```

### 4.10 RepoTree Paths

```env
REPO_TREE_SRC_PATH=/home/ubuntu/JIRA-AI
REPO_TREE_CONFIG_PATH=/home/ubuntu/JIRA-AI/repo_tree/config/repos.yaml
REPO_TREE_WORKSPACE_DIR=/home/ubuntu/JIRA-AI/repo_tree/workspace
```

For Docker deployments, override to container paths:

```env
REPO_TREE_SRC_PATH=/app
REPO_TREE_CONFIG_PATH=/app/repo_tree/config/repos.yaml
REPO_TREE_WORKSPACE_DIR=/app/repo_tree/workspace
```

### 4.11 SMTP (for emailing generated documents)

```env
SMTP_HOST=smtp.yourcompany.com
SMTP_PORT=587
SMTP_USER=ai-bot@yourcompany.com
SMTP_PASSWORD=<smtp-password>
SMTP_FROM=ai-bot@yourcompany.com
SMTP_USE_TLS=true
```

### 4.12 Google Docs (WF6 only)

```env
GOOGLE_OAUTH_CLIENT_ID=<client-id>
GOOGLE_OAUTH_CLIENT_SECRET=<client-secret>
GOOGLE_OAUTH_REFRESH_TOKEN=<refresh-token>
```

Run `python scripts/google_oauth_setup.py` to obtain the refresh token.

### 4.13 Graph Jobs & Test Case Comparison

```env
GRAPH_JOB_REPO_TIMEOUT_SECONDS=900
GRAPH_JOB_COMMIT_BATCH_SIZE=500
GRAPH_JOB_LIMIT_JIRA_ISSUES=0
GRAPH_JOB_BUILD_EMBEDDINGS=true
TEST_CASE_COMPARISON_MODEL=claude-opus-4-5
TEST_CASE_COMPARISON_MAX_TOKENS=6000
TEST_CASE_COMPARISON_PIPELINE_LIMIT=5
TEST_CASE_COMPARISON_PIPELINE_TOP_K=15
```

### 4.14 Documentation Generation Pricing

```env
DOC_PRICE_INPUT_PER_MTOK=3.0
DOC_PRICE_OUTPUT_PER_MTOK=15.0
DOC_PRICE_CACHE_READ_PER_MTOK=0.3
DOC_PRICE_CACHE_CREATE_PER_MTOK=3.75
```

For the complete list of 120+ environment variables, see `app/config.py`.

---

## 5. Docker Deployment

### 5.1 Quick Start

```bash
cp .env.example .env        # Create and fill in credentials
docker compose up -d --build
```

The API starts at `http://SERVER_IP:8000`. The admin UI is at `http://SERVER_IP:8000/graph-admin`.

### 5.2 With Local Postgres

To run a Postgres container alongside the API:

```env
DATABASE_URL=postgresql://postgres:postgres@postgres:5432/jira_ai
```

```bash
docker compose --profile local-db up -d --build
```

### 5.3 Docker-Specific Env Overrides

When running in Docker, the compose file mounts host repositories into the container at `/host-repos`. Set:

```env
DATABASE_URL=postgresql://USER:PASSWORD@host.docker.internal:5432/jira_ai
REPOSITORY_SEARCH_ROOT=/host-repos
REPOSITORY_HOST_ROOT=/home/ubuntu
REPO_TREE_SRC_PATH=/app
REPO_TREE_CONFIG_PATH=/app/repo_tree/config/repos.yaml
REPO_TREE_WORKSPACE_DIR=/app/repo_tree/workspace
```

If Ollama/Qdrant run on the host, use `host.docker.internal` in their URLs.

### 5.4 Useful Commands

```bash
docker compose logs -f jira-ai-api      # Stream logs
docker compose restart jira-ai-api      # Restart API
docker compose down                      # Stop all
docker compose build && docker compose up -d  # Rebuild after code changes
```

### 5.5 Caddy Reverse Proxy (HTTPS)

Place a `Caddyfile` on the host:

```
your-domain.com {
    reverse_proxy localhost:8000
}

your-domain.com/n8n/* {
    reverse_proxy localhost:5678
}
```

Start Caddy: `caddy start` or via systemd.

---

## 6. Jira Integration

### 6.1 Create a Jira API Token

1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Create token. Use with `JIRA_EMAIL` and `JIRA_API_TOKEN` env vars.

### 6.2 Create the AI-Approved Workflow Status

WF1 transitions approved tickets to a custom status. In your Jira project workflow:

1. Add a status named exactly as `JIRA_APPROVED_TRANSITION_NAME` (default: `LLM APPROVED`)
2. Add a transition from your initial status to this new status
3. Note the transition ID — it is used by the n8n workflow

### 6.3 Find Custom Field IDs

The platform tracks dev/QA/live due dates via Jira custom fields. To find their IDs:

```bash
curl -u YOUR_EMAIL:YOUR_TOKEN \
  "https://yourcompany.atlassian.net/rest/api/2/field" | \
  python -m json.tool | grep -A2 "due"
```

Set `JIRA_DEV_DUE_DATE_FIELD`, `JIRA_QA_DUE_DATE_FIELD`, `JIRA_LIVE_DUE_DATE_FIELD` accordingly.

### 6.4 Configure Jira Webhooks

Create webhooks in Jira (Settings → System → Webhooks) pointing to your n8n instance:

| Event | n8n Webhook URL |
|-------|----------------|
| Issue created/updated | `https://your-domain.com/n8n/webhook/jira-ticket-review` |
| Issue status changed | `https://your-domain.com/n8n/webhook/jira-status-transition` |
| Issue moved to QA | `https://your-domain.com/n8n/webhook/jira-closing-flow` |

---

## 7. Slack Integration

### 7.1 Create a Slack App

1. Go to https://api.slack.com/apps → Create New App
2. Add Bot Token Scopes: `chat:write`, `channels:read`, `groups:read`, `im:read`, `im:write`, `users:read`
3. Install to workspace. Copy the `xoxb-` Bot Token → `SLACK_BOT_TOKEN`

### 7.2 Enable Slack Events (for WF2 — thread replies)

1. In Slack App settings → Event Subscriptions → Enable
2. Request URL: `https://your-domain.com/n8n/webhook/slack-events`
3. Subscribe to: `message.im`, `message.channels`, `message.groups`

### 7.3 Populate the Channel ID Table

The `channelid_table` maps team members to their Slack DM channel IDs. This table must be populated manually for DMs and escalation routing to work:

```sql
INSERT INTO channelid_table (slack_user_name, email_id, channel_id, role, jira_account_id, display_name)
VALUES
  ('john.doe',    'john@company.com',  'D01ABCDEF', 'eng_lead',     '5f1234...', 'John Doe'),
  ('jane.smith',  'jane@company.com',  'D02GHIJKL', 'cto',          '5f5678...', 'Jane Smith'),
  ('team-channel', NULL,               'C03MNOPQR', 'team_channel', NULL,         'Engineering');
```

**Roles used for escalation routing (WF3/WF4):**

| Role | Purpose |
|------|---------|
| `eng_lead` | Engineering lead — SLA/due-date alerts |
| `cto` | CTO — escalation at 50%/25% SLA |
| `ceo` | CEO — escalation at 0% SLA |
| `team_channel` | Team channel — broadcast alerts |
| `jira_owner` | Jira ticket owner — direct notifications |

To find a user's DM channel ID, use the Slack API:

```bash
curl -H "Authorization: Bearer xoxb-YOUR-TOKEN" \
  "https://slack.com/api/conversations.open?users=U01234ABCDE" | \
  python -m json.tool
```

---

## 8. n8n Workflow Setup

### 8.1 Install n8n

Run n8n via Docker alongside the API:

```bash
docker run -d --name n8n \
  -p 5678:5678 \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n
```

### 8.2 Import Workflows

The `N8N flows/` directory contains 8 workflow JSON files. Import each one into n8n:

1. Open n8n at `https://your-domain.com/n8n/`
2. Go to Workflows → Import from File
3. Import each JSON file from `N8N flows/`

### 8.3 Update Template Placeholders

The workflow JSON files contain placeholder tokens that must be replaced with your deployment values:

| Placeholder | Replace With | Example |
|-------------|-------------|---------|
| `{{YOUR_JIRA_DOMAIN}}` | Your Jira domain | `yourcompany.atlassian.net` |
| `{{YOUR_API_DOMAIN}}` | Your API server domain | `yourcompany.com` |
| `{{YOUR_EMAIL_DOMAIN}}` | Your email domain | `yourcompany.com` |

You can do this via find-and-replace in the n8n JSON files before importing, or update the HTTP Request node URLs after import.

### 8.4 Configure n8n Credentials

After import, configure these credentials in n8n:

- **Slack account:** Add your Slack bot token (`xoxb-...`) as an API credential
- **Jira credentials:** Used in WF1 for direct Jira API calls within n8n Code nodes
- **HTTP Request nodes:** Verify all URLs point to your FastAPI instance (typically `http://localhost:8000` or `http://host.docker.internal:8000` from Docker)

### 8.5 Activate Workflows

Activate only the workflows you need. Recommended starting set:

| Workflow | Purpose | Activate? |
|----------|---------|-----------|
| WF1 — Ticket Validation | Reviews new tickets | Yes |
| WF2 — Test Case Q&A | Slack thread replies | Yes |
| WF4 — Due Date Compliance | 15-min compliance check | Yes |
| WF5 — Closing Flow | QA test case generation | Yes |
| WF3 — SLA Monitor | SLA deadline tracking | When SLA tracking is needed |
| WF5b — Dev Test Cases | Dev-phase test cases | When PR gate is needed |
| WF6 — Doc Review | Google Docs review | When Google Docs integration is set up |
| WF7 — RFT Estimates | Effort estimation | Optional |
| WF8 — Status Logger | Status transition history | Optional |

---

## 9. Database Setup

### 9.1 Create the Database

```bash
sudo -u postgres createdb jira_ai
```

Or with Docker:

```bash
docker exec -it postgres psql -U postgres -c "CREATE DATABASE jira_ai;"
```

### 9.2 Table Auto-Creation

Most tables are created automatically on first API startup using `CREATE TABLE IF NOT EXISTS`. Schema evolution uses `ALTER TABLE ADD COLUMN IF NOT EXISTS` (self-healing pattern). No migration framework (Alembic) is needed.

### 9.3 Tables Requiring Manual Setup

These tables are referenced by n8n workflows and must exist before the workflows run:

**`tickets`** — Central ticket tracking:
```sql
CREATE TABLE IF NOT EXISTS tickets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    jira_ticket_id  TEXT UNIQUE NOT NULL,
    email           TEXT,
    assigned_user_id TEXT,
    slack_channel_id TEXT,
    slack_thread_ts  TEXT,
    llm_review      TEXT,
    status          TEXT DEFAULT 'open',
    jira_payload    JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

**`messages`** — Conversation history:
```sql
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    ticket_id  UUID REFERENCES tickets(id),
    sender     TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**`channelid_table`** — Slack routing (see [Section 7.3](#73-populate-the-channel-id-table)):
```sql
CREATE TABLE IF NOT EXISTS channelid_table (
    slack_user_name TEXT PRIMARY KEY,
    email_id        TEXT,
    channel_id      TEXT NOT NULL,
    role            TEXT,
    jira_account_id TEXT,
    display_name    TEXT
);
```

**`sla_tracking`** — SLA deadline tracking:
```sql
CREATE TABLE IF NOT EXISTS sla_tracking (
    id                BIGSERIAL PRIMARY KEY,
    ticket_id         UUID REFERENCES tickets(id),
    jira_ticket_id    TEXT UNIQUE NOT NULL,
    priority          TEXT,
    assignee_slack_id TEXT,
    sla_start_time    TIMESTAMPTZ,
    sla_deadline      TIMESTAMPTZ,
    sla_window_hours  INTEGER,
    alert_75_sent     BOOLEAN DEFAULT FALSE,
    alert_50_sent     BOOLEAN DEFAULT FALSE,
    alert_25_sent     BOOLEAN DEFAULT FALSE,
    alert_0_sent      BOOLEAN DEFAULT FALSE,
    is_resolved       BOOLEAN DEFAULT FALSE
);
```

**`due_date_tracking`** — Due-date compliance:
```sql
CREATE TABLE IF NOT EXISTS due_date_tracking (
    id                  BIGSERIAL PRIMARY KEY,
    ticket_id           UUID REFERENCES tickets(id),
    jira_ticket_id      TEXT UNIQUE NOT NULL,
    priority            TEXT,
    assignee_slack_id   TEXT,
    due_date            DATE,
    tracking_start_date DATE,
    total_working_days  INTEGER,
    alert_75_sent       BOOLEAN DEFAULT FALSE,
    alert_50_sent       BOOLEAN DEFAULT FALSE,
    alert_25_sent       BOOLEAN DEFAULT FALSE,
    alert_0_sent        BOOLEAN DEFAULT FALSE,
    exceeded_alert_sent_at TIMESTAMPTZ,
    is_completed        BOOLEAN DEFAULT FALSE,
    dev_due_date        DATE,
    qa_due_date         DATE,
    live_due_date       DATE
);
```

**`test_cases`** — Generated test cases:
```sql
CREATE TABLE IF NOT EXISTS test_cases (
    id              BIGSERIAL PRIMARY KEY,
    jira_ticket_id  TEXT NOT NULL,
    tc_index        INTEGER NOT NULL,
    phase           VARCHAR(8) NOT NULL DEFAULT 'qa',
    title           TEXT,
    description     TEXT,
    steps           TEXT,
    expected_result TEXT,
    priority        TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (jira_ticket_id, phase, tc_index)
);
```

### 9.4 Run the Phase Migration (if upgrading)

If upgrading from an earlier version without `phase` support:

```bash
psql -U postgres -d jira_ai -f scripts/sql/2026-07-27_test_cases_phase.sql
```

---

## 10. Vector Search & Embeddings

### 10.1 Start Qdrant

```bash
docker run -d --name qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v qdrant_data:/qdrant/storage \
  qdrant/qdrant
```

### 10.2 Start Ollama

```bash
docker run -d --name ollama \
  -p 11434:11434 \
  -v ollama_data:/root/.ollama \
  ollama/ollama

# Pull the embedding model
docker exec ollama ollama pull bge-m3
```

### 10.3 Qdrant Collections

Collections are created automatically as needed:

| Collection | Content | Created By |
|------------|---------|------------|
| `tickets` | Ticket embeddings for similarity search | Graph jobs |
| `test_cases` | Test case embeddings for regression detection | `build_testcase_embeddings.py` |
| `rca_code_chunks` | Code chunk embeddings for RCA | RCA code index build |
| Per-repo collections | Codebase embeddings per repository | Graph jobs |

### 10.4 Build Test Case Embeddings

After test cases are generated:

```bash
python scripts/build_testcase_embeddings.py
```

---

## 11. Neo4j Code Graph

### 11.1 Start Neo4j

```bash
docker run -d --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/your-password \
  -v neo4j_data:/data \
  neo4j:5
```

### 11.2 Build the Graph

The code graph is built from locally cloned Git repositories. Clone the client's repositories under the `REPOSITORY_SEARCH_ROOT` directory, then trigger a graph build from the admin UI or via API:

```bash
curl -X POST http://localhost:8000/graph/build \
  -H "X-Service-Key: YOUR_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"action": "create_new"}'
```

The graph build indexes: Git commits, file relationships, code structure (functions, classes, imports via tree-sitter parsing), and optionally embeds Jira tickets into Qdrant.

---

## 12. Frontend Build

The React admin SPA is built during Docker image creation. For manual builds:

```bash
cd frontend
npm install
npm run build
```

Output goes to `frontend/dist/`, served by FastAPI at `/` and `/graph-admin`.

If `frontend/dist/` is missing, the API still runs and shows a placeholder page.

---

## 13. RBAC & User Management

### 13.1 Admin Seeding

On first startup, the API auto-creates an admin user from `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars. Use this account to create additional users.

### 13.2 Role Permissions

| Role | Capabilities | Default Landing Page |
|------|-------------|---------------------|
| `admin` | Everything (incl. Users page) | Dashboard |
| `developer` | repos, logs, jira, testcases, similar, docs | Dashboard |
| `qa` | jira, insights, testcases, similar, docs | Dashboard |
| `viewer` | jira, insights, logs | Dashboard |
| `documentation` | docs only | Documentation Portal |
| `usermgr` | users only (manage users without full admin) | Users page |

### 13.3 Service API Key

Server-to-server callers (n8n, webhooks) can bypass JWT auth by sending:

```
X-Service-Key: <SERVICE_API_KEY>
```

This is treated as admin-level access. Workflow endpoints that receive webhooks from n8n are unaffected.

### 13.4 Custom Role-Tab Mapping

Override the default role → capability mapping by setting `AUTH_ROLE_TABS` as a JSON string:

```env
AUTH_ROLE_TABS={"developer": ["repos", "logs", "jira", "testcases", "similar", "docs", "insights"]}
```

---

## 14. RepoTree / Repomix Setup

RepoTree is bundled into the project as `repo_architect/` plus `repo_tree/config` and `repo_tree/workspace`. It runs in-process — no separate service is needed.

### 14.1 Configure Repository List

Edit `repo_tree/config/repos.yaml` with the client's repositories:

```yaml
repos:
  - name: backend-api
    path: /home/ubuntu/backend-api
    branch: main
  - name: frontend-app
    path: /home/ubuntu/frontend-app
    branch: main
```

### 14.2 Initial Scan

Trigger an initial scan via the admin UI or API:

```bash
curl -X POST http://localhost:8000/scan/initial \
  -H "X-Service-Key: YOUR_SERVICE_KEY"
```

### 14.3 Nightly Scan

Schedule a nightly scan via cron or n8n:

```bash
curl -X POST http://localhost:8000/scan/nightly \
  -H "X-Service-Key: YOUR_SERVICE_KEY"
```

---

## 15. Optional Integrations

### 15.1 Google Docs Review (WF6)

For PRD/TechDoc review via Google Docs:

1. Create a Google Cloud project and enable the Google Docs API
2. Create OAuth 2.0 credentials (Desktop app type)
3. Run the setup script:
   ```bash
   python scripts/google_oauth_setup.py
   ```
4. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` in `.env`

### 15.2 Zoho Desk

For Zoho Desk ticket viewer:

1. Create a Zoho API client at https://api-console.zoho.com/
2. Run `python scripts/zoho_oauth_setup.py`
3. Set Zoho env vars in `.env`

### 15.3 Documentation Portal

The documentation portal lives at `/docs-portal` and supports generating four document types per repository, emailing as Word attachments, and usage/cost tracking. It requires SMTP configuration (Section 4.11) and the RepoTree setup (Section 14).

---

## 16. API Endpoints Reference

### Workflow Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/workflow1` | Ticket validation — LLM reviews ticket quality |
| POST | `/workflow2` | Slack thread reply — test case Q&A + edit |
| POST | `/workflow3/sla-check` | SLA monitoring check |
| POST | `/workflow4/due-date-check` | Due-date compliance check |
| POST | `/workflow4/aigov` | AI Governor due-date orchestration |
| POST | `/workflow5/closing` | Ticket closing flow (test case generation) |
| POST | `/workflow6/doc-review` | PRD/TechDoc review |
| POST | `/workflow7/rft-estimate` | RFT estimate report |
| POST | `/workflow8/status-transition` | Status transition logging |

### Ticket Analysis

| Method | Path | Description |
|--------|------|-------------|
| POST | `/analyze-ticket` | Analyze a single Jira ticket |
| POST | `/analyze-ticket/similar` | Find similar tickets (vector search) |
| POST | `/analyze-ticket/regression` | Find regression test cases |
| GET | `/ticket-insights/{ticket_key}` | Get ticket insights |
| POST | `/story-subtasks` | Break story into subtasks |

### Test Cases

| Method | Path | Description |
|--------|------|-------------|
| GET | `/test-cases/{ticket_key}` | Get test cases for a ticket |
| POST | `/test-cases/generate` | Generate test cases |
| POST | `/test-cases/compare` | Compare test case sets |
| GET | `/test-cases/document/{ticket_key}` | Export test cases as DOCX |
| POST | `/test-cases/embeddings/build` | Build test case embeddings |

### Jira

| Method | Path | Description |
|--------|------|-------------|
| GET | `/jira/projects` | List Jira projects |
| GET | `/jira/tickets` | Search/list tickets |
| GET | `/jira/ticket/{key}` | Get single ticket |
| POST | `/jira/fetch` | Trigger ticket fetch/cache |

### Graph & Repositories

| Method | Path | Description |
|--------|------|-------------|
| GET | `/repositories` | List discovered repos |
| POST | `/graph/build` | Start graph build job |
| GET | `/graph/jobs` | List graph jobs |
| GET | `/graph/analytics` | Get graph analytics |

### RCA (Root Cause Analysis)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/rca/run` | Start RCA investigation |
| GET | `/rca/runs` | List RCA runs |
| GET | `/rca/run/{run_id}` | Get RCA run details |
| POST | `/rca/code-index/build` | Build code index for repos |

### Documentation

| Method | Path | Description |
|--------|------|-------------|
| POST | `/docs/generate` | Generate repo documentation |
| GET | `/docs/usage` | Get doc gen usage/cost |
| POST | `/doc-review` | Review Google Doc (WF6) |

### Auth & Users

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/login` | JWT login |
| POST | `/auth/register` | Create user (admin only) |
| GET | `/auth/me` | Get current user |
| GET | `/auth/users` | List users |
| PUT | `/auth/users/{id}` | Update user |
| DELETE | `/auth/users/{id}` | Delete user |

### Other

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/settings` | Get app settings |
| PUT | `/settings` | Update settings |
| GET | `/prompts` | List available prompts |
| POST | `/email/send` | Send email via SMTP |
| GET | `/n8n/workflows` | List n8n workflows |
| GET | `/n8n/executions` | List recent n8n executions |
| GET | `/utilization` | Get ticket utilization stats |

---

## 17. n8n Workflow Reference

### WF1 — Ticket Validation

**Trigger:** Jira webhook on ticket create/update
**Flow:** Jira Webhook → Extract Fields → `POST /workflow1` → Check satisfied/unsatisfied → If satisfied: transition to approved status, set priority, assign user, insert SLA tracking → If unsatisfied: send Slack DM to reporter with review feedback, store thread context for follow-up
**LLM:** Claude Opus
**Status:** Live (partially active)

### WF2 — Test Case Q&A + Edit

**Trigger:** Slack message in thread (via Events API)
**Flow:** Slack Event → Filter bot messages → `POST /workflow2` → Reply in Slack thread
**LLM:** Claude Sonnet
**Purpose:** Handles follow-up conversations about ticket reviews and test case edits.

### WF3 — SLA Monitor

**Trigger:** Scheduled (cron)
**Flow:** `POST /workflow3/sla-check` → Checks SLA deadlines → Sends escalation alerts (75%/50%/25%/0%) to eng lead, CTO, CEO via Slack
**Status:** Not live (enable when SLA tracking is configured)

### WF4 — Due Date Compliance

**Trigger:** Scheduled every 15 minutes
**Flow:** `POST /workflow4/aigov` → Scans active tickets → Checks due dates → Sends compliance digests to Jira owner and team leads
**Status:** Live

### WF5 — Closing Flow (Phase-Aware)

**Trigger:** Jira ticket transitions to QA status
**Flow:** Detects transition → Generates QA test cases → Posts to Slack → Stores in DB
**LLM:** Claude Sonnet
**Status:** Live

### WF5b — Dev Test Cases / Code Review

**Trigger:** Jira ticket transitions to code review
**Flow:** Generates developer-focused test cases with PR gate integration
**Status:** Live

### WF6 — PRD/TechDoc Review

**Trigger:** Manual or webhook
**Flow:** Reads Google Doc → LLM reviews against quality criteria → Returns review
**Requires:** Google OAuth setup

### WF7 — RFT Estimate Report

**Trigger:** Scheduled (daily 09:00)
**Flow:** Fetches sprint tickets → LLM estimates effort → Compares with actuals → Generates report
**Status:** Not live

### WF8 — Status Transition Logger

**Trigger:** Jira webhook on status change
**Flow:** Logs every status transition to `ticket_status_history` table
**Status:** Not live

---

## 18. Database Schema Reference

PostgreSQL 16. No migration framework. Tables use `CREATE TABLE IF NOT EXISTS` (self-healing). Schema evolution uses `ALTER TABLE ADD COLUMN IF NOT EXISTS`.

### Auto-Created Tables (Python code)

These tables are created automatically on API startup:

| Table | Source File | Purpose |
|-------|-----------|---------|
| `app_users` | `app/auth.py` | User accounts and roles |
| `app_settings` | `app/app_settings.py` | Runtime app settings (key-value) |
| `jira_slack_conversations` | `app/conversation_store.py` | Slack thread ↔ Jira ticket mapping |
| `testcase_threads` | `scripts/sql/...` | Test case thread tracking |
| `channel_health_status` | `app/channel_health.py` | Slack channel health checks |
| `doc_artifacts` | `app/repo_doc_usage.py` | Generated document cache |
| `doc_generation_usage` | `app/repo_doc_usage.py` | Doc gen usage/cost tracking |
| `doc_reviews` | `app/doc_review.py` | Google Doc review results |
| `neo4j_graph_snapshots` | `app/neo4j_graph/snapshots.py` | Graph build snapshots |
| `rca_runs` | `app/rca/store.py` | RCA investigation runs |
| `rca_code_index_state` | `app/rca/code_index.py` | Code index state per repo |
| `rca_ticket_fix_links` | `app/rca/fix_links.py` | Fix commit ↔ ticket links |
| `rca_repo_map` | `app/rca/localize.py` | Component → repo mapping |
| `rft_estimate_predictions` | `app/rft_estimate_analysis.py` | LLM effort predictions |
| `story_subtasks` | `app/story_subtasks.py` | Story subtask breakdowns |
| `ticket_status_history` | `app/utilization.py` | Ticket status transitions |

### Qdrant Collections (Vector DB)

| Collection | Content |
|------------|---------|
| `tickets` | Ticket embeddings for similarity search |
| `test_cases` | Test case embeddings for regression detection |
| `rca_code_chunks` | Code chunk embeddings for RCA |
| Per-repo collections | Codebase embeddings per repository |

---

## 19. LLM Prompts

Prompts are stored as `.txt` files in the `Prompt/` directory and loaded by `PromptStore` (`app/prompt_store.py`).

### `workflow1_prompt.txt` — Ticket Validation

**Model:** Claude Opus | **Max tokens:** 1024

Instructs the LLM to act as a Jira ticket reviewer. Evaluates summary clarity, description detail, acceptance criteria, assignee, priority, and issue type. Returns JSON with `nature` (satisfied/unsatisfied), `llm_review`, `priority` (P0–P4), and `priority_explain`.

Customize this prompt with client-specific context (industry, team conventions, quality bar).

### `workflow2_prompt.txt` — Follow-up Conversation

**Model:** Claude Sonnet

Used for Slack thread conversations. Receives conversation history and ticket context, responds to user questions about ticket review feedback.

---

## 20. Troubleshooting

### API won't start

- Check `DATABASE_URL` is reachable: `psql $DATABASE_URL -c "SELECT 1"`
- Verify `.env` file exists in project root and has no syntax errors
- Check Docker logs: `docker compose logs -f jira-ai-api`

### Slack DMs not arriving

- Verify `SLACK_BOT_TOKEN` is set and the bot is installed in the workspace
- Confirm the `channelid_table` has the correct DM channel IDs for each user
- Check that the Slack app has the required scopes

### Jira webhook not triggering workflows

- Verify webhook URL in Jira points to your n8n instance
- Check n8n workflow is activated
- Test with: `curl -X POST https://your-domain.com/n8n/webhook/jira-ticket-review -d '{}'`

### Vector search returning no results

- Verify Qdrant is running: `curl http://localhost:6333/collections`
- Verify Ollama has the bge-m3 model: `curl http://localhost:11434/api/tags`
- Run a graph build job to populate embeddings

### Frontend shows "Frontend not built"

- Run `cd frontend && npm install && npm run build`
- Or rebuild the Docker image: `docker compose build`

---

## First-Time Deployment Checklist

1. **Environment:** Copy `.env.example` → `.env`, fill in all credentials
2. **Docker:** `docker compose up -d --build`
3. **Database:** Tables auto-create on startup. Run manual SQL for `channelid_table` population
4. **Admin:** Log in at `/graph-admin` with `ADMIN_EMAIL`/`ADMIN_PASSWORD`
5. **Jira:** Configure webhooks pointing to n8n
6. **Slack:** Create bot app, populate `channelid_table` with team DM channels
7. **n8n:** Import workflows from `N8N flows/`, replace `{{YOUR_*}}` placeholders, configure Slack credential, activate WF1/WF2/WF4/WF5
8. **Repositories:** Clone client repos under `REPOSITORY_SEARCH_ROOT`
9. **RepoTree:** Edit `repo_tree/config/repos.yaml`, run initial scan
10. **Vector search:** Start Qdrant + Ollama, pull bge-m3 model, run graph build with embeddings
11. **Neo4j:** Start Neo4j, trigger graph build from admin UI
12. **Google Docs (optional):** Run `scripts/google_oauth_setup.py`
13. **SMTP (optional):** Configure for document email delivery
