"""Unified database schema management and initialization for JIRA-AI.

Consolidates all table creation, indexes, and migrations into a single,
clean, idempotent module matching the exact application schema.
"""

from __future__ import annotations

import logging
import psycopg
from app.config import Settings

log = logging.getLogger(__name__)

ALL_TABLES_SQL = """
-- 1. App Users & RBAC
CREATE TABLE IF NOT EXISTS app_users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_app_users_email ON app_users (LOWER(email));

-- 2. App Settings (Key-Value)
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Jira Projects
CREATE TABLE IF NOT EXISTS jira_projects (
    key TEXT PRIMARY KEY,
    name TEXT,
    project_type TEXT,
    description TEXT,
    lead_name TEXT,
    lead_email TEXT,
    data JSONB,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 4. Jira Ticket Cache
CREATE TABLE IF NOT EXISTS jira_ticket_cache (
    ticket_key TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    summary TEXT,
    description TEXT,
    status TEXT,
    issue_type TEXT,
    priority TEXT,
    assignee_email TEXT,
    assignee_name TEXT,
    reporter_email TEXT,
    reporter_name TEXT,
    labels JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE,
    data JSONB,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_jira_ticket_cache_project ON jira_ticket_cache (UPPER(project_key));
CREATE INDEX IF NOT EXISTS idx_jira_ticket_cache_fetched ON jira_ticket_cache (fetched_at);
CREATE INDEX IF NOT EXISTS idx_jira_ticket_cache_status ON jira_ticket_cache (status);

-- 5. Jira Fetch Log
CREATE TABLE IF NOT EXISTS jira_fetch_log (
    id SERIAL PRIMARY KEY,
    project_key TEXT,
    ticket_count INTEGER,
    from_cache BOOLEAN,
    force_refresh BOOLEAN,
    duration_ms INTEGER,
    error TEXT,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 6. Slack Conversations (Thread ↔ Ticket Mapping)
CREATE TABLE IF NOT EXISTS jira_slack_conversations (
    id SERIAL PRIMARY KEY,
    slack_thread_ts TEXT UNIQUE NOT NULL,
    slack_channel_id TEXT NOT NULL,
    issue_key TEXT NOT NULL,
    history JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conversations_issue_key ON jira_slack_conversations (issue_key);

-- 7. Channel Health Status
CREATE TABLE IF NOT EXISTS channel_health_status (
    channel_id TEXT PRIMARY KEY,
    ok BOOLEAN NOT NULL DEFAULT TRUE,
    error TEXT,
    message_ts TEXT,
    last_checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 8. Ticket Status History (Status Transition Logger / Utilization)
CREATE TABLE IF NOT EXISTS ticket_status_history (
    id BIGSERIAL PRIMARY KEY,
    jira_ticket_id TEXT NOT NULL,
    project_key TEXT,
    issue_type TEXT,
    from_status TEXT,
    to_status TEXT NOT NULL,
    assignee_name TEXT,
    source TEXT,
    changed_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_history_jira_id ON ticket_status_history (jira_ticket_id);
CREATE INDEX IF NOT EXISTS idx_status_history_changed ON ticket_status_history (changed_at);

-- 9. RFT Estimate Predictions
CREATE TABLE IF NOT EXISTS rft_estimate_predictions (
    jira_ticket_id TEXT PRIMARY KEY,
    content_hash TEXT,
    original_seconds BIGINT,
    predicted_hours DOUBLE PRECISION,
    confidence TEXT,
    rationale TEXT,
    flag TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    reason TEXT,
    explanation TEXT
);

-- 10. Story Subtasks Breakdowns
CREATE TABLE IF NOT EXISTS story_subtasks (
    story_key TEXT PRIMARY KEY,
    subtasks JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 11. Document Artifacts Cache
CREATE TABLE IF NOT EXISTS doc_artifacts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    repo TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    context_hash TEXT,
    filename TEXT,
    markdown TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd NUMERIC DEFAULT 0,
    created_by TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_doc_artifacts_repo ON doc_artifacts (repo, doc_type);

-- 12. Document Generation Usage & Cost Tracking
CREATE TABLE IF NOT EXISTS doc_generation_usage (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    user_email TEXT,
    repo TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    reused BOOLEAN DEFAULT FALSE,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd NUMERIC DEFAULT 0,
    context_hash TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 13. Document Reviews (PRD / TechDoc Review)
CREATE TABLE IF NOT EXISTS doc_reviews (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    issue_key TEXT NOT NULL,
    doc_url TEXT NOT NULL,
    score NUMERIC(5, 2),
    verdict TEXT,
    review_details JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_doc_reviews_issue ON doc_reviews (issue_key);

-- 14. Neo4j Graph Snapshots
CREATE TABLE IF NOT EXISTS neo4j_graph_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    node_count INTEGER DEFAULT 0,
    relationship_count INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 15. RCA (Root Cause Analysis) Runs
CREATE TABLE IF NOT EXISTS rca_runs (
    run_id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    user_email TEXT,
    jira_key TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    localized_repos JSONB NOT NULL DEFAULT '[]'::jsonb,
    candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnosis JSONB,
    confidence REAL,
    agent_trace JSONB NOT NULL DEFAULT '[]'::jsonb,
    document JSONB,
    error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rca_runs_key ON rca_runs (jira_key);

-- 16. RCA Code Index State
CREATE TABLE IF NOT EXISTS rca_code_index_state (
    repo TEXT PRIMARY KEY,
    commit_sha TEXT NOT NULL,
    chunk_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 17. RCA Ticket Fix Links
CREATE TABLE IF NOT EXISTS rca_ticket_fix_links (
    id BIGSERIAL PRIMARY KEY,
    ticket_key TEXT NOT NULL,
    repo TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    changed_files JSONB NOT NULL DEFAULT '[]'::jsonb,
    source TEXT NOT NULL DEFAULT 'git',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (ticket_key, repo, commit_sha)
);
CREATE INDEX IF NOT EXISTS idx_rca_fix_links_key ON rca_ticket_fix_links (ticket_key);

-- 18. RCA Component Repo Map
CREATE TABLE IF NOT EXISTS rca_repo_map (
    id BIGSERIAL PRIMARY KEY,
    component TEXT NOT NULL,
    repo TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (component, repo)
);
CREATE INDEX IF NOT EXISTS idx_rca_repo_map_component ON rca_repo_map (LOWER(component));

-- 19. Ring Studio Generations (Optional Plugin)
CREATE TABLE IF NOT EXISTS ring_studio_generations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    image_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    style TEXT,
    file_path TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 20. Graph Build Jobs
CREATE TABLE IF NOT EXISTS graph_jobs (
    job_id UUID PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    user_email TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    repos_total INTEGER DEFAULT 0,
    repos_done INTEGER DEFAULT 0,
    jira_total INTEGER DEFAULT 0,
    jira_done INTEGER DEFAULT 0,
    totals JSONB DEFAULT '{}'::jsonb,
    progress JSONB DEFAULT '{}'::jsonb,
    logs JSONB DEFAULT '[]'::jsonb,
    error_msg TEXT,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_graph_jobs_started ON graph_jobs (started_at DESC);

-- 21. GitHub Repositories & Pull Logs
CREATE TABLE IF NOT EXISTS github_repositories (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    full_name TEXT,
    container_path TEXT UNIQUE NOT NULL,
    host_path TEXT,
    remote_url TEXT,
    branch TEXT,
    current_commit TEXT,
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_pull_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    job_id UUID,
    repo_name TEXT,
    container_path TEXT,
    success BOOLEAN,
    output TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 22. App User Settings (Per-User / Tenant Scoped Key-Value Isolation)
CREATE TABLE IF NOT EXISTS app_user_settings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
    user_email TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, key)
);
CREATE INDEX IF NOT EXISTS idx_app_user_settings_user ON app_user_settings (user_id);
CREATE INDEX IF NOT EXISTS idx_app_user_settings_email ON app_user_settings (LOWER(user_email));
"""

MIGRATIONS_SQL = """
DO $$
BEGIN
    -- Add user_id / user_email columns to existing tables if missing
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'graph_jobs') THEN
        ALTER TABLE graph_jobs ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE;
        ALTER TABLE graph_jobs ADD COLUMN IF NOT EXISTS user_email TEXT;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'doc_artifacts') THEN
        ALTER TABLE doc_artifacts ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'doc_generation_usage') THEN
        ALTER TABLE doc_generation_usage ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'rca_runs') THEN
        ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE;
        ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS user_email TEXT;
    END IF;
END $$;
"""

POST_MIGRATION_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_doc_artifacts_user ON doc_artifacts (user_id);
CREATE INDEX IF NOT EXISTS idx_doc_usage_user ON doc_generation_usage (user_id);
CREATE INDEX IF NOT EXISTS idx_rca_runs_user ON rca_runs (user_id);
CREATE INDEX IF NOT EXISTS idx_graph_jobs_user ON graph_jobs (user_id);
"""


def init_db(settings: Settings) -> None:
    """Initialize all tables, constraints, and indexes idempotently."""
    if not settings.database_url:
        log.warning("DATABASE_URL is not set; skipping database initialization")
        return

    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(ALL_TABLES_SQL)
                cur.execute(MIGRATIONS_SQL)
                cur.execute(POST_MIGRATION_INDEXES_SQL)
            conn.commit()
        log.info("Database schema initialized successfully (all tables verified and user-scoped)")
    except Exception as exc:
        log.exception("Database initialization failed: %s", exc)
        raise
