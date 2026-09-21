"""Pydantic models for the HTTP API.

These define the request/response contracts shown in the auto-generated
OpenAPI docs at /docs. Keep them stable — changing field names is a
breaking change for any client.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ============================================================
# Job models (shared by scan endpoints)
# ============================================================

class JobResponse(BaseModel):
    """Returned immediately when an async job is created."""
    job_id: str
    kind: str
    status: str
    created_at: str
    poll_url: str = Field(..., description="GET this URL to check job status")


class JobDetail(BaseModel):
    """Full job state, returned by GET /jobs/{id}."""
    id: str
    kind: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    progress: Optional[str] = None


# ============================================================
# Scan endpoints (no request body needed — config drives everything)
# ============================================================

class InitialScanRequest(BaseModel):
    """Optional knobs for an initial scan."""
    force: bool = Field(
        False,
        description="Run even if state.json already has SHAs (overwrites existing maps).",
    )


class NightlySyncRequest(BaseModel):
    """Optional knobs for a nightly sync."""
    no_git_pull: bool = Field(
        False,
        description="Skip `git fetch && pull` for each repo (use when another process manages clones).",
    )


class RepomixReindexRequest(BaseModel):
    """Refresh packed Repomix XML used as testcase source context."""
    model_config = ConfigDict(populate_by_name=True)

    issue_key: Optional[str] = Field(None, alias="issueKey")
    project_key: Optional[str] = Field(None, alias="projectKey")
    repo_url: Optional[str] = Field(
        None,
        alias="repoUrl",
        description="Optional Git remote URL; when present, only matching configured repos are packed.",
    )
    repos: Optional[List[str]] = Field(
        None,
        description="Optional configured repo names to pack. Takes precedence over repoUrl.",
    )
    no_git_pull: bool = Field(
        False,
        alias="noGitPull",
        description="Skip `git fetch && pull` before packing.",
    )
    force: bool = Field(
        False,
        description="Pack even when the Repomix state says the current HEAD is already packed.",
    )


# ============================================================
# Testcase generation endpoint
# ============================================================

class TicketInput(BaseModel):
    """JIRA ticket fields. Map your JIRA API response to this."""
    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(..., examples=["PROJ-1234"])
    summary: str
    issue_type: str = Field("", alias="issueType", examples=["Bug"])
    description: str = ""
    acceptance_criteria: str = Field("", alias="acceptanceCriteria")
    labels: List[str] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list)


class GenerateTestCasesRequest(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def accept_flat_n8n_ticket_payload(cls, data):
        """Accept older n8n payloads that sent JIRA fields at the top level."""
        if not isinstance(data, dict) or data.get("ticket") is not None:
            return data

        issue_key = data.get("issueKey") or data.get("key")
        summary = data.get("summary")
        if not issue_key or summary is None:
            return data

        copied = dict(data)
        copied["ticket"] = {
            "key": issue_key,
            "summary": summary,
            "issueType": data.get("issueType", ""),
            "description": data.get("description", ""),
            "acceptanceCriteria": data.get("acceptanceCriteria", ""),
            "labels": data.get("labels") or [],
            "components": data.get("components") or [],
        }
        return copied

    ticket: TicketInput
    style: Literal["gherkin", "pytest", "junit", "plain"] = "plain"
    audience: Literal["qa", "dev"] = Field(
        "qa",
        description="Persona for the generated cases: 'qa' (functional QA test cases) "
                    "or 'dev' (implementation-level cases for a developer at Code Review).",
    )
    repos: Optional[List[str]] = Field(
        None,
        description="Override auto-detection — provide explicit repo names. "
                    "If omitted, the service picks the most relevant repos automatically.",
    )
    embedding_model: Literal["codebase_bge_m3", "codebase_qwen3_0_6b", "codebase_mxbai_large"] = Field(
        "codebase_bge_m3",
        description="Qdrant collection/model key used for semantic source-file retrieval.",
    )
    top_k: int = Field(
        15,
        ge=1,
        le=50,
        description="Number of Qdrant semantic source hits to retrieve.",
    )
    include_semantic_context: bool = Field(
        False,
        description="When true, fetch Qdrant hits and include them with Repomix context.",
    )
    pr_context: Optional[str] = Field(
        None,
        description="Pre-fetched PR diff blob (changed files + unified diffs) for the "
                    "developer flow. When provided it is injected as authoritative "
                    "'latest code under review' context, ahead of the indexed maps.",
    )


class GenerateTestCasesResponse(BaseModel):
    ticket_key: str
    grounded_repos: List[str] = Field(
        ...,
        description="Which repos' architecture maps were used as context.",
    )
    style: str
    testcases: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Parsed test cases for automation workflows; `test_cases` remains the full markdown output.",
    )
    test_cases: str = Field(..., description="LLM-generated test cases in the requested format.")
    semantic_hits_count: int = 0
    files_touched_count: int = 0
    architecture_context_chars: int = 0
    repomix_context_chars: int = 0
