"""Application configuration loaded from environment variables.

This file centralizes settings such as prompt folder path, default prompt,
LLM provider, model name, API keys, and timeout values so the rest of the
project does not read environment variables directly.
"""

import os
from dataclasses import dataclass
from urllib.parse import quote_plus

from dotenv import load_dotenv


load_dotenv(encoding="utf-8-sig")


def _threshold_from_env(env_name: str, default: float) -> float:
    """Parse a 0–1 (or 0–100 with/without %) similarity threshold from env."""
    raw = os.getenv(env_name, str(default)).strip()
    if raw.endswith("%"):
        raw = raw[:-1].strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    if value > 1:
        value = value / 100
    return min(max(value, 0.0), 1.0)


def _similar_ticket_match_threshold() -> float:
    return _threshold_from_env("SIMILAR_TICKET_MATCH_THRESHOLD", 0.68)


def _regression_match_threshold() -> float:
    # Cross-domain match (ticket text vs test-case text) tends to score a little
    # lower than ticket-vs-ticket, so this defaults slightly below the similar
    # ticket threshold. Override with REGRESSION_MATCH_THRESHOLD.
    return _threshold_from_env("REGRESSION_MATCH_THRESHOLD", 0.62)


@dataclass
class Settings:
    prompt_dir: str = os.getenv("PROMPT_DIR", "Prompt")
    default_prompt: str = os.getenv("DEFAULT_PROMPT", "ticket_prompt")
    llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic")
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    testcase_chat_model: str = os.getenv(
        "TESTCASE_CHAT_MODEL",
        os.getenv("ANTHROPIC_MODEL", os.getenv("LLM_MODEL", "claude-sonnet-4-6")),
    )
    test_case_comparison_model: str = os.getenv("TEST_CASE_COMPARISON_MODEL", "claude-opus-4-5")
    test_case_comparison_max_tokens: int = int(os.getenv("TEST_CASE_COMPARISON_MAX_TOKENS", "6000"))
    test_case_comparison_pipeline_limit: int = int(os.getenv("TEST_CASE_COMPARISON_PIPELINE_LIMIT", "5"))
    test_case_comparison_pipeline_top_k: int = int(os.getenv("TEST_CASE_COMPARISON_PIPELINE_TOP_K", "15"))
    llm_timeout_seconds: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    llm_test_case_timeout_seconds: int = int(os.getenv("LLM_TEST_CASE_TIMEOUT_SECONDS", "300"))
    database_url_override: str = os.getenv("DATABASE_URL", "")
    db_host: str = os.getenv("DB_HOST", "")
    db_port: str = os.getenv("DB_PORT", "5432")
    db_name: str = os.getenv("DB_NAME", "")
    db_user: str = os.getenv("DB_USER", "")
    db_password: str = os.getenv("DB_PASSWORD", "")
    slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
    slack_default_channel_id: str = os.getenv("SLACK_DEFAULT_CHANNEL_ID", "")

    # AI Governor scheduled notification (Workflow: governor-notify). The n8n
    # cron workflow calls POST /workflow/governor-notify and posts the returned
    # message to this channel. Override the text via env; `{date}` in the message
    # is replaced with the current date.
    governor_notify_channel_id: str = os.getenv("GOVERNOR_NOTIFY_CHANNEL_ID", "")
    governor_notify_message: str = os.getenv(
        "GOVERNOR_NOTIFY_MESSAGE",
        ":robot_face: *AI Governor daily check-in* — {date}\n"
        "All systems nominal. Reply in thread to flag anything for review.",
    )
    # RFT estimate report (WF7): the Jira project to scan for tickets that have
    # an Original Estimate filled. Posts to GOVERNOR_NOTIFY_CHANNEL_ID.
    rft_estimate_project_key: str = os.getenv("RFT_ESTIMATE_PROJECT_KEY", "RFT")
    # Max ticket rows listed in the Slack message before collapsing to a count.
    rft_estimate_max_rows: int = int(os.getenv("RFT_ESTIMATE_MAX_ROWS", "40"))
    # Restrict the report to the active sprint. Default ON: adds
    # `sprint in openSprints()` to the JQL. Set to false to scan the whole
    # project regardless of sprint.
    rft_estimate_current_sprint_only: bool = os.getenv(
        "RFT_ESTIMATE_CURRENT_SPRINT_ONLY", "true"
    ).lower() in {"1", "true", "yes", "on"}
    # Pin to one specific sprint id (e.g. "284") instead of openSprints().
    # Blank = use openSprints() when current_sprint_only is on.
    rft_estimate_sprint_id: str = os.getenv("RFT_ESTIMATE_SPRINT_ID", "")
    # LLM-backed estimate analysis: predict the realistic effort (for an average
    # experienced developer) per ticket and flag drift vs the Original Estimate.
    rft_estimate_analyze: bool = os.getenv(
        "RFT_ESTIMATE_ANALYZE", "true"
    ).lower() in {"1", "true", "yes", "on"}
    # Flag a ticket when predicted vs original deviates by more than this percent.
    rft_estimate_flag_threshold_pct: int = int(os.getenv("RFT_ESTIMATE_FLAG_THRESHOLD_PCT", "25"))
    # Cap LLM analyses per run (uncached tickets) to bound cost/runtime.
    # 0 (or negative) = unlimited — analyze every eligible ticket.
    rft_estimate_max_analyze: int = int(os.getenv("RFT_ESTIMATE_MAX_ANALYZE", "0"))
    # Skip tickets whose Original Estimate is <= this many working hours — they
    # are too small for estimate-drift analysis. Default 8h (= 1 working day),
    # so only tickets estimated at more than a day are analyzed/reported.
    rft_estimate_min_hours: float = float(os.getenv("RFT_ESTIMATE_MIN_HOURS", "8"))
    # WF7 benchmark persona — the developer the LLM estimates for. Default is an
    # average developer on THIS team (not a FAANG dev), so the "should-have" time
    # reflects realistic delivery for the people who actually do the work.
    rft_estimate_benchmark_persona: str = os.getenv(
        "RFT_ESTIMATE_BENCHMARK_PERSONA",
        "an average developer on this team — competent and familiar with the "
        "stack, but not necessarily fast or senior, and new to this specific "
        "ticket",
    )
    # History calibration: blend the LLM estimate with (Original Estimate ×
    # the team's historical actual/estimate ratio) so the benchmark is grounded
    # in how this team really performs. 0 = LLM only, 1 = history only.
    rft_estimate_history_weight: float = float(os.getenv("RFT_ESTIMATE_HISTORY_WEIGHT", "0.5"))
    rft_estimate_history_lookback_days: int = int(os.getenv("RFT_ESTIMATE_HISTORY_LOOKBACK_DAYS", "180"))
    rft_estimate_history_min_samples: int = int(os.getenv("RFT_ESTIMATE_HISTORY_MIN_SAMPLES", "8"))
    rft_estimate_factor_min: float = float(os.getenv("RFT_ESTIMATE_FACTOR_MIN", "0.5"))
    rft_estimate_factor_max: float = float(os.getenv("RFT_ESTIMATE_FACTOR_MAX", "3.0"))
    # WF7 Slack target. Defaults to GOVERNOR_NOTIFY_CHANNEL_ID so existing setups
    # are unchanged; set RFT_ESTIMATE_CHANNEL_ID to route WF7 somewhere else
    # (e.g. a test channel) without affecting the governor-notify flow.
    rft_estimate_channel_id: str = os.getenv(
        "RFT_ESTIMATE_CHANNEL_ID",
        os.getenv("GOVERNOR_NOTIFY_CHANNEL_ID", ""),
    )
    # WF7: ground estimates in the Neo4j code graph (complexity/coupling/churn of
    # the code relevant to each ticket). Falls back to repograph if unavailable.
    rft_estimate_use_graph: bool = os.getenv(
        "RFT_ESTIMATE_USE_GRAPH", "true"
    ).lower() in {"1", "true", "yes", "on"}
    jira_base_url: str = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    jira_email: str = os.getenv("JIRA_EMAIL", "")
    jira_api_token: str = os.getenv("JIRA_API_TOKEN", "")
    jira_project_keys: str = os.getenv("JIRA_PROJECT_KEYS", "")
    jira_project_key: str = os.getenv("JIRA_PROJECT_KEYS", "")
    jira_excluded_project_keys: str = os.getenv("JIRA_EXCLUDED_PROJECT_KEYS", "")
    jira_approved_transition_name: str = os.getenv("JIRA_APPROVED_TRANSITION_NAME", "")

    # Workflow4 batch alerts — Jira custom-field IDs for per-phase due dates.
    # Leave blank to fall back to the system `duedate` field.
    jira_dev_due_date_field: str = os.getenv("JIRA_DEV_DUE_DATE_FIELD", "")
    jira_qa_due_date_field: str = os.getenv("JIRA_QA_DUE_DATE_FIELD", "")
    jira_live_due_date_field: str = os.getenv("JIRA_LIVE_DUE_DATE_FIELD", "")
    # Comma/space-separated Jira project keys whose Stories the story→subtask
    # mapper should scan. Blank = all visible projects minus JIRA_EXCLUDED_PROJECT_KEYS.
    story_subtask_project_keys: str = os.getenv("STORY_SUBTASK_PROJECT_KEYS", "")
    # Restrict the PRODUCTION Workflow4 batches (fetch + due-date scan + story
    # mapping) to these project keys. Blank = all projects minus JIRA_EXCLUDED_PROJECT_KEYS.
    workflow4_project_keys: str = os.getenv("WORKFLOW4_PROJECT_KEYS", "")
    # Whether the PRODUCTION Workflow4 batches rebuild Qdrant embeddings on each run.
    workflow4_build_embeddings: bool = os.getenv(
        "WORKFLOW4_BUILD_EMBEDDINGS", "false"
    ).lower() in {"1", "true", "yes", "on"}
    # Lowercased Jira status names that select the active phase. Anything not
    # matched here is treated as the Dev phase (the default).
    jira_qa_statuses: str = os.getenv(
        "JIRA_QA_STATUSES",
        "ready for qa,ready to qa,in qa,in qa (staging),ready for preprod,in qa (preprod),qa in progress,in review",
    )
    jira_live_statuses: str = os.getenv(
        "JIRA_LIVE_STATUSES",
        "release ready,ready for deployment,ready for release,in deployment,deploying,5% deployed,100% deployed",
    )
    repograph_base_url: str = os.getenv("REPOGRAPH_BASE_URL", "http://127.0.0.1:8088").rstrip("/")
    repo_tree_base_url: str = os.getenv(
        "REPO_TREE_BASE_URL",
        os.getenv("REPOTREE_BASE_URL", ""),
    ).rstrip("/")
    repo_tree_timeout_seconds: int = int(os.getenv("REPO_TREE_TIMEOUT_SECONDS", "300"))
    external_request_timeout_seconds: int = int(os.getenv("EXTERNAL_REQUEST_TIMEOUT_SECONDS", "15"))
    # GitHub (dev test cases): at Code Review the PR branch holds the latest code.
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_api_base: str = os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
    github_org: str = os.getenv("GITHUB_ORG", "")
    # Caps so a large PR can't blow up the generation prompt.
    github_pr_max_files: int = int(os.getenv("GITHUB_PR_MAX_FILES", "60"))
    github_pr_per_file_patch_chars: int = int(os.getenv("GITHUB_PR_PER_FILE_PATCH_CHARS", "8000"))
    github_pr_context_max_chars: int = int(os.getenv("GITHUB_PR_CONTEXT_MAX_CHARS", "120000"))
    n8n_graph_webhook_url: str = os.getenv("N8N_GRAPH_WEBHOOK_URL", "")
    n8n_api_key: str = os.getenv("N8N_API_KEY", "")
    # Base URL of the running n8n instance (no trailing /api).
    n8n_base_url: str = os.getenv("N8N_BASE_URL", "").rstrip("/")
    n8n_monitor_execution_window: int = int(os.getenv("N8N_MONITOR_EXECUTION_WINDOW", "250"))
    n8n_monitor_timeout_seconds: int = int(os.getenv("N8N_MONITOR_TIMEOUT_SECONDS", "20"))
    repository_search_root: str = os.getenv("REPOSITORY_SEARCH_ROOT", ".")
    repository_host_root: str = os.getenv("REPOSITORY_HOST_ROOT", os.getenv("REPOSITORY_SEARCH_ROOT", "."))
    excluded_repository_names: str = os.getenv("EXCLUDED_REPOSITORY_NAMES", "JIRA-AI")
    graph_job_repo_timeout_seconds: int = int(os.getenv("GRAPH_JOB_REPO_TIMEOUT_SECONDS", "3600"))
    graph_job_commit_batch_size: int = int(os.getenv("GRAPH_JOB_COMMIT_BATCH_SIZE", "500"))
    graph_job_limit_jira_issues: int = int(os.getenv("GRAPH_JOB_LIMIT_JIRA_ISSUES", "0"))
    graph_job_build_embeddings: bool = os.getenv("GRAPH_JOB_BUILD_EMBEDDINGS", "true").lower() in {"1", "true", "yes", "on"}
    graph_job_repo_concurrency: int = int(os.getenv("GRAPH_JOB_REPO_CONCURRENCY", "4"))

    # Neo4j code knowledge graph (built from active repos by app/neo4j_graph).
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://jira-ai-neo4j:7687" if os.path.exists("/.dockerenv") else "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", os.getenv("NEO4J_USERNAME", "neo4j"))
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "password")
    neo4j_database: str = os.getenv("NEO4J_DATABASE", "neo4j")
    neo4j_activity_min_score: int = int(os.getenv("NEO4J_ACTIVITY_MIN_SCORE", "1"))
    neo4j_max_commits_per_repo: int = int(os.getenv("NEO4J_MAX_COMMITS_PER_REPO", "3000"))
    neo4j_max_file_bytes: int = int(os.getenv("NEO4J_MAX_FILE_BYTES", "1500000"))
    neo4j_write_batch_size: int = int(os.getenv("NEO4J_WRITE_BATCH_SIZE", "1000"))
    neo4j_build_pull_latest: bool = os.getenv(
        "NEO4J_BUILD_PULL_LATEST", "true"
    ).lower() in {"1", "true", "yes", "on"}

    # Qdrant (vector store for embeddings)
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")

    # Ollama (local embedding model)
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_embed_model: str = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")
    ollama_embed_timeout_seconds: int = int(os.getenv("OLLAMA_EMBED_TIMEOUT_SECONDS", "600"))
    ollama_embed_batch_size: int = int(os.getenv("OLLAMA_EMBED_BATCH_SIZE", "4"))
    ollama_embed_concurrency: int = int(os.getenv("OLLAMA_EMBED_CONCURRENCY", "1"))
    codebase_embed_max_chars: int = int(os.getenv("CODEBASE_EMBED_MAX_CHARS", "2000"))
    codebase_embed_max_files_per_repo: int = int(os.getenv("CODEBASE_EMBED_MAX_FILES_PER_REPO", "1000"))

    # Jira ticket cache TTL in hours (0 = always re-fetch)
    jira_cache_ttl_hours: int = int(os.getenv("JIRA_CACHE_TTL_HOURS", "1"))
    similar_ticket_match_threshold: float = _similar_ticket_match_threshold()
    regression_match_threshold: float = _regression_match_threshold()

    # Authentication / RBAC
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_expire_minutes: int = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))
    admin_email: str = os.getenv("ADMIN_EMAIL", "")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    service_api_key: str = os.getenv("SERVICE_API_KEY", "")

    # SMTP (for emailing generated documents)
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", ""))
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
    smtp_use_ssl: bool = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}

    # ── RCA (AI Root Cause Analysis) ────────────────────────────────────────
    rca_repo_root: str = os.getenv(
        "RCA_REPO_ROOT",
        os.getenv("REPOSITORY_SEARCH_ROOT", "."),
    )
    # Qdrant collection holding function/class-level code chunks (Phase A).
    # Distinct from the file-level `codebase_bge_m3` collection.
    rca_code_chunks_collection: str = os.getenv("RCA_CODE_CHUNKS_COLLECTION", "code_chunks")
    # Repos always skipped by the RCA code-index build (comma-separated names),
    # e.g. very large repos that dominate build time. Editable per-build in the UI.
    rca_index_excluded_repos: str = os.getenv("RCA_INDEX_EXCLUDED_REPOS", "")
    # Largest source file (bytes) the chunker will parse; mirrors the graph cap.
    rca_chunk_max_file_bytes: int = int(os.getenv("RCA_CHUNK_MAX_FILE_BYTES", "1500000"))
    # Models for the extraction (Phase C) and synthesis (Phase G) calls.
    rca_extract_model: str = os.getenv("RCA_EXTRACT_MODEL", os.getenv("LLM_MODEL", "claude-sonnet-4-6"))
    rca_synthesis_model: str = os.getenv("RCA_SYNTHESIS_MODEL", os.getenv("LLM_MODEL", "claude-sonnet-4-6"))
    rca_agent_model: str = os.getenv("RCA_AGENT_MODEL", os.getenv("LLM_MODEL", "claude-sonnet-4-6"))
    # Hard caps for the agentic investigation loop (Phase F). The token cap counts
    # cumulative *uncached* input+output; with prompt caching the re-sent context
    # is billed as cache reads, so a full investigation fits comfortably. Keep the
    # ceiling high enough that the agent concludes rather than being cut off mid-run
    # (a cut-off investigation yields no evidence → "INSUFFICIENT DATA").
    rca_agent_max_iterations: int = int(os.getenv("RCA_AGENT_MAX_ITERATIONS", "16"))
    rca_agent_max_tokens: int = int(os.getenv("RCA_AGENT_MAX_TOKENS", "500000"))
    # Confidence at/above which a diagnosis is auto-delivered; below routes to
    # the human-review queue (Phase H).
    rca_confidence_threshold: float = float(os.getenv("RCA_CONFIDENCE_THRESHOLD", "0.6"))
    # Diagnosis-first: a concrete suggested fix is emitted ONLY at/above this
    # confidence; below it the resolution names the suspected area only. The fix
    # is always advisory (human-review), never applied.
    rca_confidence_threshold_for_fix: float = float(
        os.getenv("RCA_CONFIDENCE_THRESHOLD_FOR_FIX", "0.95")
    )
    # Post the diagnosis back to Jira as a comment. OFF by default (dry-run):
    # above-threshold runs store the comment but do not call Jira.
    rca_jira_post_enabled: bool = os.getenv(
        "RCA_JIRA_POST_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}

    # LLM pricing for document-generation cost estimates (USD per 1M tokens).
    # Defaults are Claude Sonnet 4.x list prices; override per deployment.
    doc_price_input_per_mtok: float = float(os.getenv("DOC_PRICE_INPUT_PER_MTOK", "3.0"))
    doc_price_output_per_mtok: float = float(os.getenv("DOC_PRICE_OUTPUT_PER_MTOK", "15.0"))
    doc_price_cache_read_per_mtok: float = float(os.getenv("DOC_PRICE_CACHE_READ_PER_MTOK", "0.30"))
    doc_price_cache_write_per_mtok: float = float(os.getenv("DOC_PRICE_CACHE_WRITE_PER_MTOK", "3.75"))

    # USD→INR rate for displaying costs in rupees. Costs are computed and stored
    # in USD; this only affects what users see. Override per deployment to track
    # the current exchange rate.
    usd_to_inr: float = float(os.getenv("USD_TO_INR", "95.75"))

    # ── Zoho Desk (customer ticket visibility) ───────────────────────────────
    # OAuth self-client credentials + org id for the Zoho Desk REST API. The
    # "zoho" tab looks a customer up by email/phone and lists their tickets.
    # Data-center domains default to India (.in); switch to .com/.eu/etc. per
    # deployment. Leaving credentials blank degrades the tab gracefully.
    zoho_client_id: str = os.getenv("ZOHO_CLIENT_ID", "")
    zoho_client_secret: str = os.getenv("ZOHO_CLIENT_SECRET", "")
    zoho_refresh_token: str = os.getenv("ZOHO_REFRESH_TOKEN", "")
    zoho_org_id: str = os.getenv("ZOHO_ORG_ID", "")
    zoho_accounts_base: str = os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.in").rstrip("/")
    zoho_desk_base: str = os.getenv("ZOHO_DESK_BASE", "https://desk.zoho.in").rstrip("/")

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override

        if not all([self.db_host, self.db_port, self.db_name, self.db_user]):
            return ""

        username = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        credentials = f"{username}:{password}" if password else username
        return f"postgresql://{credentials}@{self.db_host}:{self.db_port}/{self.db_name}"

    def apply_dynamic_overrides(self) -> None:
        """Load and apply dynamic configuration overrides from app_settings table."""
        try:
            from app.app_settings import get_all_settings
            db_settings = get_all_settings(self)
            for k, v in db_settings.items():
                if v is None:
                    continue
                v_str = str(v).strip()
                if not v_str:
                    continue
                # Map db key (e.g. jira_base_url) to Settings field
                if hasattr(self, k):
                    curr_val = getattr(self, k)
                    if isinstance(curr_val, bool):
                        setattr(self, k, v_str.lower() in {"1", "true", "yes", "on"})
                    elif isinstance(curr_val, int):
                        try:
                            setattr(self, k, int(v_str))
                        except ValueError:
                            pass
                    elif isinstance(curr_val, float):
                        try:
                            setattr(self, k, float(v_str))
                        except ValueError:
                            pass
                    else:
                        setattr(self, k, v_str)
        except Exception as exc:  # noqa: BLE001 - never block startup
            import logging
            logging.getLogger(__name__).debug("apply_dynamic_overrides skipped: %s", exc)


settings = Settings()


def reload_settings() -> Settings:
    """Reload dynamic overrides into the global settings singleton."""
    settings.apply_dynamic_overrides()
    return settings

