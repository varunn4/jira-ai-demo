"""Pydantic request and response models for the FastAPI layer.

This file defines the structured API contracts used by endpoints, including
the ticket-analysis request body, model-output response, and prompt-listing
response.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AnalyzeTicketRequest(BaseModel):
    ticket_data: Dict[str, Any] = Field(..., description="Jira ticket metadata JSON.")


# ── Dev test-case PR gate ────────────────────────────────────────────────────
# Dev (Code Review) test cases must be generated against the PR under review,
# not the indexed main branch. The gate reads/asks-for the PR URL in Jira;
# pr-context pulls the PR diff to inject into /testcases/generate.
class DevPrGateRequest(BaseModel):
    issueKey: str = Field(..., description="Jira issue key, e.g. RFT-2184.")


class DevPrGateResponse(BaseModel):
    status: Literal["ready", "awaiting_pr"] = Field(
        ..., description="'ready' when a PR URL is known; 'awaiting_pr' when we asked for it."
    )
    issueKey: str
    prUrl: Optional[str] = None
    owner: Optional[str] = None
    repo: Optional[str] = None
    number: Optional[int] = None
    commentPosted: bool = False
    reason: Optional[str] = None


class DevPrContextRequest(BaseModel):
    prUrl: str = Field(..., description="GitHub PR URL, e.g. https://github.com/your-org/your-repo/pull/482.")


class DevPrContextResponse(BaseModel):
    repo: str
    owner: str
    number: int
    prUrl: str
    title: str = ""
    headRef: str = ""
    headSha: str = ""
    baseRef: str = ""
    changedFiles: List[str] = Field(default_factory=list)
    contextText: str = Field(..., description="PR diff blob to pass as pr_context into /testcases/generate.")


class AnalyzeTicketResponse(BaseModel):
    status: str
    review: str


class PromptListResponse(BaseModel):
    prompts: List[str]


class Workflow1ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    issueKey: str = ""
    summary: str = ""
    description: str = ""
    assignee: str = ""
    dueDate: str = ""
    createdAt: str = ""
    priority: str = ""
    issueType: str = ""
    status: str = ""
    reporter: str = ""
    github_repo_url: Optional[str] = ""
    github_pat: Optional[str] = ""
    target_repo: Optional[str] = ""


class Workflow1ReviewResponse(BaseModel):
    assignee_channel_id: str
    reporter_channel_id: str
    nature: str
    llm_review: str
    priority: str
    missing_fields: List[str] = Field(default_factory=list)
    github_repo: Optional[str] = ""


class Workflow2ReplyRequest(BaseModel):
    slack_thread_ts: str = ""
    slack_channel_id: str = ""
    user_message: str = ""
    user_id: str = ""


class Workflow2ReplyResponse(BaseModel):
    reply: str
    slack_thread_ts: str
    slack_channel_id: str


class Workflow3SlackAlert(BaseModel):
    channel_id: str
    message: str


class Workflow3SLAResponse(BaseModel):
    status: str
    tickets_checked: int
    alerts_sent: int
    resolved_tickets: int
    alerts: List[Workflow3SlackAlert]


class Workflow4SlackAlert(BaseModel):
    channel_id: str
    message: str


class Workflow4DueDateResponse(BaseModel):
    status: str
    tickets_checked: int
    alerts_sent: int
    completed_tickets: int
    alerts: List[Workflow4SlackAlert]


# ── MoM-2 batch alerts (09:00 assignee / 15:00 TL) ───────────────────────────
class AlertItem(BaseModel):
    channel_id: str
    message: str
    blocks: Optional[List[Any]] = None


class AlertBatchResponse(BaseModel):
    alerts: List[AlertItem] = []
    alerts_sent: int = 0


# ── WF7 RFT estimate report — admin sprint selection ─────────────────────────
class RftSprintSettingRequest(BaseModel):
    value: str = Field(
        ...,
        description="'open' (current sprint), a numeric sprint id, or 'all' (no filter).",
    )


# ── Status-transition log (utilization analytics) ───────────────────────────
class TransitionRecordRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    issueKey: str
    toStatus: str = Field(..., alias="toStatus")
    fromStatus: str = ""
    projectKey: str = ""
    issueType: str = ""
    assignee: str = ""
    changedAt: str = ""


class TransitionRecordResponse(BaseModel):
    recorded: bool = False
    ticket: str | None = None
    to_status: str | None = None
    reason: str | None = None


# ── MoM-3 document review ────────────────────────────────────────────────────
class DocReviewRequest(BaseModel):
    issueKey: str
    description: Any = ""          # plain string OR Jira ADF dict


class DocReviewResponse(BaseModel):
    issueKey: str
    reviewed: int = 0
    unchanged: int = 0
    skipped: List[Dict[str, Any]] = []
    commentPosted: bool = False
    reason: str | None = None


# ── MoM-4 test-case document attachment ──────────────────────────────────────
class TestCaseDocRequest(BaseModel):
    issueKey: str
    summary: str = ""
    testcases: List[Dict[str, Any]] = []
    phase: Literal["qa", "dev"] = Field(
        default="qa",
        description="Which flow the doc belongs to: 'qa' (Ready for QA, default) or "
                    "'dev' (Code Review). Controls the filename, comment marker, and labels.",
    )


class TestCaseDocResponse(BaseModel):
    issueKey: str
    attached: bool = False
    skipped: bool = False
    attachmentId: str | None = None
    filename: str | None = None
    commentPosted: bool = False
    reason: str | None = None


class SlackMessageRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    userid: str
    channelId: str
    threadid: str | None = None
    user_message: str = Field(..., alias="user message")


class SlackMessageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    userid: str
    llm_reply: str = Field(..., alias="LLM reply")


class JiraReviewWorkflowRequest(BaseModel):
    ticket_data: Dict[str, Any] = Field(..., description="Jira ticket metadata JSON.")
    slack_channel_id: str | None = Field(
        default=None,
        description="Slack channel/DM id where review messages should be posted.",
    )


class JiraReviewWorkflowResponse(BaseModel):
    jira_issue_key: str
    status: str
    review: str
    slack_thread_ts: str | None = None
    slack_sent: bool = False
    jira_update: Dict[str, Any] | None = None
    model_output: Dict[str, Any]


class SlackReplyRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="user")
    channel_id: str = Field(..., alias="channel")
    thread_ts: str
    text: str
    event_ts: str | None = None


class SlackReplyResponse(BaseModel):
    jira_issue_key: str
    status: str
    review: str
    slack_thread_ts: str
    slack_sent: bool
    jira_update: Dict[str, Any] | None = None
    model_output: Dict[str, Any]


class Workflow2Request(BaseModel):
    user_id: str | None = None
    slack_channel_id: str
    slack_thread_ts: str
    user_message: str
    phase: Literal["qa", "dev"] = Field(
        default="qa",
        description="Which test-case set the Q&A thread operates on: 'qa' (default) or "
                    "'dev' (developer cases from the Code Review flow).",
    )


class Workflow2Response(BaseModel):
    slack_channel_id: str
    slack_thread_ts: str
    reply: str


class GraphAdminTriggerRequest(BaseModel):
    action: Literal["update", "regenerate", "create_new", "jira_tickets_only"]
    repositories: List[str] = []
    pull_latest_code: bool = True
    fetch_latest_jira_tickets: bool = True
    include_jira_tickets: bool = True
    build_embeddings: bool = True
    embedding_model: Literal["codebase_bge_m3", "codebase_qwen3_0_6b", "codebase_mxbai_large"] = "codebase_bge_m3"
    notes: str | None = None


class GraphAdminTriggerResponse(BaseModel):
    job_id: str
    action: str
    status: str
    repository_count: int
    excluded_repositories: List[str]


class GraphJobResponse(BaseModel):
    job_id: str
    action: str
    status: str
    totals: Dict[str, int]
    progress: Dict[str, int]
    logs: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    error: str | None = None
    started_at: str
    completed_at: str | None = None


class Neo4jBuildRequest(BaseModel):
    repositories: List[str] = Field(
        default_factory=list,
        description="Active repo names to (re)build. Empty = all active repos.",
    )
    wipe_mode: str = Field(
        default="managed",
        description="'managed' (clear code/git labels, keep Jira), 'all' (clear whole DB), or 'none'.",
    )
    include_code: bool = Field(
        default=True,
        description="Parse source with tree-sitter for the Class/Function/CALLS layer.",
    )
    pull_latest: bool = Field(
        default=True,
        description="git pull --ff-only every repo before building (latest code + activity).",
    )


class Neo4jBuildResponse(BaseModel):
    job_id: str
    status: str
    repository_count: int


class RepomixReindexRequest(BaseModel):
    repositories: List[str] = Field(
        default_factory=list,
        description="Repository names to repack. Empty = all configured RepoMix repos.",
    )
    pull_latest_code: bool = Field(
        default=True, description="Run git pull before repacking each repo."
    )
    force: bool = Field(
        default=False,
        description="Repack even when the repo HEAD is unchanged since the last pack.",
    )


class RepomixReindexResponse(BaseModel):
    selected: List[str]
    packed: List[str] = []
    skipped: List[str] = []
    failed: List[Dict[str, Any]] = []
    unknown: List[str] = []
    details: List[Dict[str, Any]] = []


class CodeAnalysisReportRequest(BaseModel):
    repositories: List[str]
    include_graph_context: bool = True
    embedding_model: Literal["codebase_bge_m3", "codebase_qwen3_0_6b", "codebase_mxbai_large"] = "codebase_bge_m3"
    format: Literal["pdf", "markdown"] = "pdf"



class RepoDocRequest(BaseModel):
    repo: str = Field(..., description="Configured RepoTree repository name.")
    doc_type: str = Field(
        default="onboarding_guide",
        description="Document type id, e.g. 'onboarding_guide', 'architecture', "
                    "'engineering_scorecard', or 'technical_audit'.",
    )


class RepoDocExportRequest(BaseModel):
    markdown: str = Field(..., description="Markdown document body to convert.")
    filename: str = Field(
        default="document",
        description="Base filename (without extension) for the exported file.",
    )


class RepoDocEmailRequest(BaseModel):
    job_id: str = Field(..., description="Completed repo-doc job id to email.")
    to_email: str = Field(
        ...,
        description="Recipient email address(es), comma or space separated.",
    )


class TestCaseRequest(BaseModel):
    ticket_data: Dict[str, Any] = Field(..., description="JIRA ticket metadata JSON.")
    repo: str | None = Field(
        default=None,
        description="Repo full_name to restrict semantic search (e.g. 'owner/repo'). "
                    "None = search across all repos.",
    )
    embedding_model: Literal["codebase_bge_m3", "codebase_qwen3_0_6b", "codebase_mxbai_large"] = Field(
        default="codebase_bge_m3",
        description="Embedding model used for semantic search.",
    )
    style: Literal["gherkin", "pytest", "junit", "plain"] = Field(
        default="plain",
        description="Output format requested from RepoTree.",
    )
    audience: Literal["qa", "dev"] = Field(
        default="qa",
        description="Persona for the cases: 'qa' (functional QA test cases, default) or "
                    "'dev' (implementation-level cases for a developer at Code Review).",
    )
    top_k: int = Field(default=15, ge=1, le=50, description="Number of semantic hits to retrieve.")


class TestCaseResponse(BaseModel):
    ticket_key: str
    test_cases: str = Field(..., description="Markdown test-case document generated by Claude.")
    semantic_hits_count: int
    functions_found: int
    files_touched_count: int
    grounded_repos: list[str] = []
    style: str = "plain"
    architecture_context_chars: int = 0
    repomix_context_chars: int = 0


class SimilarTicketRequest(BaseModel):
    summary: str = Field(..., description="Summary of the new ticket.")
    description: str | None = Field(default=None, description="Full ticket description.")
    project_key: str | None = Field(default=None, description="Restrict search to a project key.")


class SimilarTicketResult(BaseModel):
    ticket_key: str
    project_key: str
    summary: str
    description: str | None = None
    status: str
    issue_type: str | None = None
    priority: str | None = None
    assignee_name: str | None = None
    reporter_name: str | None = None
    labels: list[str] = []
    created_at: str | None = None
    updated_at: str | None = None
    similarity_score: float = 0.0


class SimilarTicketsResponse(BaseModel):
    query_summary: str
    total_found: int
    search_method: str = Field(..., description="'hybrid_rrf', 'semantic', 'keyword_fallback', or 'none'")
    tickets: list[SimilarTicketResult]


# ─── Test-case regression flag ───────────────────────────────────────────────


class TestCaseRegressionRequest(BaseModel):
    summary: str = Field(..., description="Summary of the new ticket.")
    description: str | None = Field(default=None, description="Full ticket description.")
    project_key: str | None = Field(default=None, description="Restrict search to a project key.")


class TestCaseRegressionMatch(BaseModel):
    jira_ticket_id: str = Field(..., description="Ticket the matched test case belongs to.")
    project_key: str = ""
    phase: str = Field("qa", description="'qa' or 'dev' test-case set.")
    tc_index: int | None = None
    title: str = ""
    steps: list[str] = []
    expected: str | None = None
    status: str = Field("", description="Test-case status (currently always 'pending').")
    ticket_summary: str | None = None
    ticket_status: str | None = None
    similarity_score: float = 0.0


class TestCaseRegressionResponse(BaseModel):
    query_summary: str
    total_found: int
    search_method: str = Field(..., description="'semantic', 'keyword_fallback', or 'none'")
    matches: list[TestCaseRegressionMatch]


# ─── n8n workflow monitoring ─────────────────────────────────────────────────


class N8nWorkflowRow(BaseModel):
    id: str
    name: str
    active: bool = Field(..., description="True if the workflow is live/published in n8n.")
    tags: list[str] = []
    created_at: str | None = None
    updated_at: str | None = None
    executions: int = Field(0, description="Runs seen within the sampled execution window.")
    success: int = 0
    errors: int = 0
    other: int = Field(0, description="Waiting/running/canceled/other outcomes in the window.")
    last_status: str | None = None
    last_run_at: str | None = None


class N8nMonitorTotals(BaseModel):
    workflows: int = 0
    active: int = 0
    inactive: int = 0
    executions: int = 0
    errors: int = 0
    success: int = 0


class N8nMonitorResponse(BaseModel):
    configured: bool = Field(..., description="False when N8N_BASE_URL / N8N_API_KEY are unset.")
    base_url: str | None = None
    execution_window: int = Field(..., description="Max recent executions sampled for counts.")
    executions_sampled: int = 0
    workflows: list[N8nWorkflowRow] = []
    totals: N8nMonitorTotals = N8nMonitorTotals()


class ChannelHealthCheckRequest(BaseModel):
    channel_ids: list[str] | None = Field(
        None,
        description="Specific channel IDs to probe. When omitted, the full "
        "discovered inventory (role map + assignee DMs + env channels) is swept.",
    )
    message: str | None = Field(
        None,
        description="Override the probe message text sent to each channel.",
    )
