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

-- 23. Tickets (Workflow Central Tracking)
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
CREATE INDEX IF NOT EXISTS idx_tickets_jira_id ON tickets (jira_ticket_id);
CREATE INDEX IF NOT EXISTS idx_tickets_slack_thread ON tickets (slack_thread_ts);

-- 24. Messages (Conversation History per Ticket)
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    ticket_id  TEXT,
    sender     TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_messages_ticket_id ON messages (ticket_id);

-- 25. Channel ID Table (Slack User & Channel Mapping)
CREATE TABLE IF NOT EXISTS channelid_table (
    slack_user_name TEXT PRIMARY KEY,
    slack_user_id   TEXT,
    email_id        TEXT,
    channel_id      TEXT NOT NULL,
    role            TEXT,
    jira_account_id TEXT,
    display_name    TEXT
);

-- 26. SLA Tracking
CREATE TABLE IF NOT EXISTS sla_tracking (
    id                BIGSERIAL PRIMARY KEY,
    ticket_id         TEXT,
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

-- 27. Due Date Tracking
CREATE TABLE IF NOT EXISTS due_date_tracking (
    id                  BIGSERIAL PRIMARY KEY,
    ticket_id           TEXT,
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

-- 28. Test Cases
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
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS test_cases_jira_phase_tc_uidx ON test_cases (jira_ticket_id, phase, tc_index);

-- 29. Test Case Threads (Slack Thread -> Phase Map)
CREATE TABLE IF NOT EXISTS testcase_threads (
    slack_channel_id TEXT NOT NULL,
    slack_thread_ts  TEXT NOT NULL,
    jira_ticket_id   TEXT NOT NULL,
    phase            VARCHAR(8) NOT NULL DEFAULT 'qa',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (slack_channel_id, slack_thread_ts)
);

-- 30. User Memory & Conversation Context Store
CREATE TABLE IF NOT EXISTS user_memory (
    id              SERIAL PRIMARY KEY,
    user_identifier TEXT NOT NULL,
    context_type    TEXT NOT NULL,
    data            JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_identifier, context_type)
);
CREATE INDEX IF NOT EXISTS idx_user_memory_user ON user_memory (user_identifier);

-- 31. Ticket Creation Claims (concurrent duplicate guard)
-- Duplicate detection reads committed rows, so two identical submissions racing
-- each other both saw an empty result and both created a ticket. A claim is taken
-- on (repository + normalised summary) before the check runs; the loser is told a
-- matching request is already in flight. Rows are short-lived and self-expiring.
CREATE TABLE IF NOT EXISTS ticket_creation_claims (
    claim_key   TEXT PRIMARY KEY,
    claimed_by  TEXT,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ticket_claims_claimed_at ON ticket_creation_claims (claimed_at);

-- 32. Effort Tracking (WF3 timeline clock + WF4 estimate review)
-- Deliberately separate from sla_tracking so enabling SLA monitoring later cannot
-- collide with estimate tracking; the two measure different clocks.
CREATE TABLE IF NOT EXISTS effort_tracking (
    id                  BIGSERIAL PRIMARY KEY,
    jira_ticket_id      TEXT UNIQUE NOT NULL,
    project_key         TEXT,
    summary             TEXT,
    assignee_name       TEXT,
    assignee_slack_id   TEXT,
    estimate_hours      NUMERIC,
    predicted_hours     NUMERIC,
    drift_pct           NUMERIC,
    drift_flagged_at    TIMESTAMPTZ,
    estimate_chased_at  TIMESTAMPTZ,
    tracking_started_at TIMESTAMPTZ,
    elapsed_hours       NUMERIC DEFAULT 0,
    last_checkin_at     TIMESTAMPTZ,
    alert_50_sent       BOOLEAN DEFAULT FALSE,
    alert_25_sent       BOOLEAN DEFAULT FALSE,
    alert_breached_sent BOOLEAN DEFAULT FALSE,
    actual_hours        NUMERIC,
    closed_at           TIMESTAMPTZ,
    is_complete         BOOLEAN DEFAULT FALSE,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_effort_tracking_open ON effort_tracking (is_complete, tracking_started_at);
CREATE INDEX IF NOT EXISTS idx_effort_tracking_assignee ON effort_tracking (assignee_slack_id);
"""

MIGRATIONS_SQL = """
DO $$
DECLARE
    _tbl TEXT;
    _con TEXT;
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
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'test_cases') THEN
        ALTER TABLE test_cases ADD COLUMN IF NOT EXISTS phase VARCHAR(8) NOT NULL DEFAULT 'qa';
    END IF;

    -- Legacy ticket_id decoupling.
    -- Older deployments created tickets.id as SERIAL (integer) while this schema
    -- declares UUID, so any FK from messages/sla_tracking/due_date_tracking to
    -- tickets(id) is either unbuildable or the wrong type. Workflow1/Workflow2 now
    -- address rows by Jira key ("GOV-4") as well as by row id, so these columns are
    -- plain TEXT with no FK. Drop the constraints and widen the columns in place.
    FOR _tbl IN SELECT unnest(ARRAY['messages', 'sla_tracking', 'due_date_tracking']) LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = _tbl) THEN
            FOR _con IN
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                WHERE rel.relname = _tbl AND con.contype = 'f'
            LOOP
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', _tbl, _con);
            END LOOP;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = _tbl AND column_name = 'ticket_id' AND data_type <> 'text'
            ) THEN
                EXECUTE format('ALTER TABLE %I ALTER COLUMN ticket_id TYPE TEXT USING ticket_id::text', _tbl);
            END IF;
        END IF;
    END LOOP;

    -- channelid_table had no column holding the Slack user id (only the display
    -- name), so an inbound Slack event could not be resolved to the email that
    -- user_memory is keyed by. Populate it to give workflow2 per-user memory.
    -- The deployed table also predates display_name and jira_account_id, which this
    -- schema declares; CREATE TABLE IF NOT EXISTS silently skipped them.
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'channelid_table') THEN
        ALTER TABLE channelid_table ADD COLUMN IF NOT EXISTS slack_user_id TEXT;
        ALTER TABLE channelid_table ADD COLUMN IF NOT EXISTS display_name TEXT;
        ALTER TABLE channelid_table ADD COLUMN IF NOT EXISTS jira_account_id TEXT;
        ALTER TABLE channelid_table ADD COLUMN IF NOT EXISTS email_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_channelid_slack_user_id ON channelid_table (slack_user_id);
    END IF;

    -- jira_slack_conversations had two conflicting definitions in this codebase
    -- (app/db/schema.py used issue_key/history, app/conversation_store.py used
    -- jira_issue_key/messages). Whichever ran first won, and the other side's
    -- queries failed. Make both column sets present so either reader works.
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'jira_slack_conversations') THEN
        ALTER TABLE jira_slack_conversations ADD COLUMN IF NOT EXISTS issue_key TEXT;
        ALTER TABLE jira_slack_conversations ADD COLUMN IF NOT EXISTS jira_issue_key TEXT;
        ALTER TABLE jira_slack_conversations ADD COLUMN IF NOT EXISTS history JSONB DEFAULT '[]'::jsonb;
        ALTER TABLE jira_slack_conversations ADD COLUMN IF NOT EXISTS messages JSONB DEFAULT '[]'::jsonb;
        ALTER TABLE jira_slack_conversations ADD COLUMN IF NOT EXISTS original_ticket_data JSONB DEFAULT '{}'::jsonb;
        ALTER TABLE jira_slack_conversations ADD COLUMN IF NOT EXISTS previous_review JSONB DEFAULT '{}'::jsonb;
        ALTER TABLE jira_slack_conversations ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open';
        ALTER TABLE jira_slack_conversations ALTER COLUMN issue_key DROP NOT NULL;
        ALTER TABLE jira_slack_conversations ALTER COLUMN jira_issue_key DROP NOT NULL;
        UPDATE jira_slack_conversations SET issue_key = jira_issue_key WHERE issue_key IS NULL;
        UPDATE jira_slack_conversations SET jira_issue_key = issue_key WHERE jira_issue_key IS NULL;
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
