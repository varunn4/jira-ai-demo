# AI Governor

AI-powered Jira ticket governance platform. Automates ticket validation, test case generation, SLA monitoring, due-date compliance, document review, and root cause analysis across your Jira projects.

## Key Capabilities

- **Ticket Validation (WF1):** LLM reviews every new/updated ticket for quality, assigns priority, and routes feedback via Slack DMs
- **Test Case Generation (WF5/5b):** Automatically generates QA and dev test cases when tickets move to QA or code review
- **SLA & Due-Date Monitoring (WF3/WF4):** Tracks SLA deadlines and due-date compliance with tiered escalation alerts
- **Test Case Q&A (WF2):** Conversational Slack thread interface for discussing and editing generated test cases
- **Document Review (WF6):** Reviews PRDs and tech docs from Google Docs against quality criteria
- **Root Cause Analysis:** Agentic investigation pipeline that localizes bugs to specific code files
- **Code Knowledge Graph:** Neo4j graph of commits, files, functions, and ticket relationships
- **Similar Ticket Detection:** Vector-based similarity search across ticket history
- **Documentation Portal:** Generates and emails repository documentation as Word files

## Tech Stack

Python 3.12 / FastAPI, React 18 / Vite 5, PostgreSQL 16, Qdrant, Neo4j, Ollama, n8n, Docker, Caddy

## Quick Start

```bash
cp .env.example .env        # Fill in credentials
docker compose up -d --build
```

Admin UI: `http://SERVER_IP:8000/graph-admin`

## Documentation

See **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)** for the complete setup and configuration guide, including environment variables, Jira/Slack integration, n8n workflow import, database schema, and API reference.
