"""FastAPI Router for Jira tickets, caching, analysis, similarity, and regression."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth import CurrentUser, require_tab
from app.config import settings
from app.exceptions import LLMConfigurationError, PromptNotFoundError
from app.jira_ticket_insights import scan_jira_ticket_cache
from app.json_utils import parse_model_json, review_status, review_text
from app.llm_client import build_llm_client
from app.prompt_store import PromptStore
from app.schemas import (
    AnalyzeTicketRequest,
    AnalyzeTicketResponse,
    SimilarTicketRequest,
    SimilarTicketResult,
    SimilarTicketsResponse,
    TestCaseRegressionMatch,
    TestCaseRegressionRequest,
    TestCaseRegressionResponse,
    TestCaseRequest,
    TestCaseResponse,
)
from app.similar_ticket_finder import SimilarTicketFinder
from app.test_case_generator import TestCaseGenerator
from app.testcase_regression_finder import TestCaseRegressionFinder
from app.ticket_analyzer import TicketAnalyzer

log = logging.getLogger(__name__)

router = APIRouter(tags=["Jira"])
prompt_store = PromptStore(settings.prompt_dir)


def _excluded_jira_projects() -> list[str]:
    return [
        key.strip().upper()
        for chunk in settings.jira_excluded_project_keys.split(",")
        for key in chunk.split()
        if key.strip()
    ]


def _jira_project_where_clause(project_key: str | None = None, q: str | None = None) -> tuple[str, tuple[Any, ...]]:
    where_parts: list[str] = []
    params: list[Any] = []
    clean_project_key = project_key.strip().upper() if project_key else ""
    if clean_project_key:
        where_parts.append("UPPER(project_key) = %s")
        params.append(clean_project_key)

    search_text = q.strip() if q else ""
    if search_text:
        search_pattern = f"%{search_text}%"
        where_parts.append(
            "(ticket_key ILIKE %s OR summary ILIKE %s OR project_key ILIKE %s OR status ILIKE %s OR issue_type ILIKE %s OR priority ILIKE %s OR assignee_name ILIKE %s OR assignee_email ILIKE %s)"
        )
        params.extend([search_pattern] * 8)

    excluded = _excluded_jira_projects()
    if excluded:
        placeholders = ", ".join(["%s"] * len(excluded))
        where_parts.append(f"UPPER(project_key) NOT IN ({placeholders})")
        params.extend(excluded)

    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    return where, tuple(params)


@router.get("/graph-admin/jira-tickets")
def graph_admin_jira_tickets(
    limit: int = 100,
    offset: int = 0,
    project_key: str | None = None,
    q: str | None = None,
    force_refresh: bool = False,
    _user: CurrentUser = Depends(require_tab("jira")),
) -> dict[str, Any]:
    log.debug("GET /graph-admin/jira-tickets project=%s q=%s limit=%d offset=%d force_refresh=%s", project_key, q, limit, offset, force_refresh)
    from app.config import reload_settings
    current_settings = reload_settings()

    if not current_settings.database_url:
        return {
            "count": 0,
            "tickets": [],
            "excluded_projects": _excluded_jira_projects(),
            "error": "DATABASE_URL not configured",
        }
    try:
        import psycopg
        from psycopg.rows import dict_row

        where, base_params = _jira_project_where_clause(project_key=project_key, q=q)
        with psycopg.connect(current_settings.database_url, row_factory=dict_row) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM jira_ticket_cache {where}",
                base_params,
            ).fetchone()
            count = row["n"] if row else 0

            # Auto-fetch if cache is empty or user requested force_refresh
            if (count == 0 or force_refresh) and current_settings.jira_base_url and current_settings.jira_email and current_settings.jira_api_token:
                try:
                    from app.jira_fetcher import fetch_all_tickets
                    log.info("Jira cache empty or force_refresh requested in graph_admin_jira_tickets; syncing from Jira Cloud...")
                    fetch_all_tickets(force_refresh=True)
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n FROM jira_ticket_cache {where}",
                        base_params,
                    ).fetchone()
                    count = row["n"] if row else 0
                except Exception as sync_exc:
                    log.warning("Failed auto-syncing Jira tickets: %s", sync_exc)

            list_params = (*base_params, limit, offset)
            tickets = conn.execute(
                f"""
                SELECT ticket_key, project_key, summary, status, issue_type, priority, assignee_name,
                       updated_at, fetched_at
                FROM jira_ticket_cache
                {where}
                ORDER BY updated_at DESC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                list_params,
            ).fetchall()
            log.info("Returning %d of %d jira tickets (project=%s, q=%s)", len(tickets), count, project_key, q)
            return {
                "count": count,
                "tickets": [dict(t) for t in tickets],
                "excluded_projects": _excluded_jira_projects(),
            }
    except Exception as exc:
        log.error("Failed to query jira_ticket_cache: %s", exc)
        return {
            "count": 0,
            "tickets": [],
            "excluded_projects": _excluded_jira_projects(),
            "error": str(exc),
        }


@router.get("/graph-admin/fetch-logs")
def graph_admin_fetch_logs(
    limit: int = 100,
    _user: CurrentUser = Depends(require_tab("logs")),
) -> dict[str, Any]:
    log.debug("GET /graph-admin/fetch-logs limit=%d", limit)
    if not settings.database_url:
        return {"logs": [], "error": "DATABASE_URL not configured"}
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            logs = conn.execute(
                """
                SELECT project_key, ticket_count, from_cache, force_refresh,
                       duration_ms, error, fetched_at
                FROM jira_fetch_log
                ORDER BY fetched_at DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            log.debug("Returning %d fetch log entries", len(logs))
            return {"logs": [dict(l) for l in logs]}
    except Exception as exc:
        log.error("Failed to query jira_fetch_log: %s", exc)
        return {"logs": [], "error": str(exc)}


@router.get("/graph-admin/jira-ticket-insights")
def graph_admin_jira_ticket_insights(
    project_key: str | None = None,
    match_type: str = "all",
    limit: int = 500,
    force_refresh: bool = False,
    _user: CurrentUser = Depends(require_tab("insights")),
) -> dict[str, Any]:
    log.debug(
        "GET /graph-admin/jira-ticket-insights project=%s match_type=%s limit=%d force_refresh=%s",
        project_key,
        match_type,
        limit,
        force_refresh,
    )
    from app.config import reload_settings
    current_settings = reload_settings()

    try:
        clean_project_key = project_key.strip().upper() if project_key else None
        res = scan_jira_ticket_cache(
            current_settings,
            project_key=clean_project_key or None,
            match_type=match_type,
            limit=max(1, min(limit, 1000)),
            excluded_project_keys=_excluded_jira_projects(),
        )

        # If cache is empty or user requested force_refresh, fetch tickets from Jira Cloud
        if (res.get("total_tickets", 0) == 0 or force_refresh) and current_settings.jira_base_url and current_settings.jira_email and current_settings.jira_api_token:
            try:
                from app.jira_fetcher import fetch_all_tickets
                log.info("Jira cache empty or refresh requested: fetching live tickets from Jira Cloud...")
                fetch_all_tickets(force_refresh=True)
                res = scan_jira_ticket_cache(
                    current_settings,
                    project_key=clean_project_key or None,
                    match_type=match_type,
                    limit=max(1, min(limit, 1000)),
                    excluded_project_keys=_excluded_jira_projects(),
                )
            except Exception as exc:
                log.warning("Live Jira fetch encountered error: %s", exc)

        return res
    except Exception as exc:
        log.error("Failed to scan jira_ticket_cache: %s", exc)
        return {
            "total_tickets": 0,
            "matching_tickets": 0,
            "returned_tickets": 0,
            "counts": {},
            "tickets": [],
            "error": str(exc),
        }


@router.post("/analyze-ticket", response_model=AnalyzeTicketResponse)
def analyze_ticket(request: AnalyzeTicketRequest) -> AnalyzeTicketResponse:
    ticket_key = request.ticket_data.get("key") or request.ticket_data.get("issue_key") or "unknown"
    log.info("POST /analyze-ticket key=%s", ticket_key)
    try:
        analyzer = TicketAnalyzer(
            settings=settings,
            prompt_store=prompt_store,
            llm_client=build_llm_client(settings),
        )
        result = analyzer.analyze(
            ticket_data=request.ticket_data,
            prompt_name=settings.default_prompt,
        )
        try:
            model_json = parse_model_json(result["model_output"])
        except json.JSONDecodeError as exc:
            log.error("LLM returned non-JSON for ticket %s", ticket_key)
            raise HTTPException(
                status_code=502,
                detail="LLM returned non-JSON output. Use a prompt that returns only JSON.",
            ) from exc

        status = review_status(model_json)
        log.info("Ticket %s analysis complete: status=%s", ticket_key, status)
        return AnalyzeTicketResponse(status=status, review=review_text(model_json))

    except PromptNotFoundError as exc:
        log.error("Prompt not found: %s", exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    except LLMConfigurationError as exc:
        log.error("LLM configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/analyze-ticket/test-cases", response_model=TestCaseResponse)
def generate_test_cases(
    request: TestCaseRequest,
    _user: CurrentUser = Depends(require_tab("testcases")),
) -> TestCaseResponse:
    """
    Generate test cases for a JIRA ticket using:
      1. RepoTree's Repomix-derived repository context
      2. Qdrant semantic code hits fetched by RepoTree
      3. LLM to synthesise a Markdown test-case document
    """
    ticket_key = (
        request.ticket_data.get("issueKey")
        or request.ticket_data.get("key")
        or request.ticket_data.get("issue_key")
        or "unknown"
    )
    log.info(
        "POST /analyze-ticket/test-cases key=%s repo=%s model=%s style=%s audience=%s",
        ticket_key, request.repo, request.embedding_model, request.style, request.audience,
    )
    try:
        generator = TestCaseGenerator(settings=settings)
        result = generator.generate(
            request.ticket_data,
            repo=request.repo,
            embedding_model=request.embedding_model,
            top_k=request.top_k,
            style=request.style,
            audience=request.audience,
        )
    except LLMConfigurationError as exc:
        log.error("LLM configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Test case generation failed for ticket %s", ticket_key)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    log.info(
        "Test cases generated for %s: semantic=%d repos=%d files=%d",
        ticket_key, result["semantic_hits_count"],
        result["functions_found"], result["files_touched_count"],
    )
    return TestCaseResponse(
        ticket_key=ticket_key,
        test_cases=result["test_cases"],
        semantic_hits_count=result["semantic_hits_count"],
        functions_found=result["functions_found"],
        files_touched_count=result["files_touched_count"],
        grounded_repos=result.get("grounded_repos") or [],
        style=result.get("style") or request.style,
        architecture_context_chars=result.get("architecture_context_chars") or 0,
        repomix_context_chars=result.get("repomix_context_chars") or 0,
    )


@router.post("/analyze-ticket/similar", response_model=SimilarTicketsResponse)
def find_similar_tickets(
    request: SimilarTicketRequest,
    _user: CurrentUser = Depends(require_tab("similar")),
) -> SimilarTicketsResponse:
    """Find historically similar Jira tickets for a new incoming ticket."""
    log.info(
        "POST /analyze-ticket/similar summary=%r project=%s",
        request.summary[:60], request.project_key,
    )
    try:
        finder = SimilarTicketFinder(settings=settings)
        result = finder.find_similar(
            request.summary,
            request.description,
            project_key=request.project_key,
        )
    except Exception as exc:
        log.exception("Similar ticket search failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    tickets = [
        SimilarTicketResult(
            ticket_key=t.get("ticket_key", ""),
            project_key=t.get("project_key", ""),
            summary=t.get("summary", ""),
            description=t.get("description"),
            status=t.get("status", ""),
            issue_type=t.get("issue_type"),
            priority=t.get("priority"),
            assignee_name=t.get("assignee_name"),
            reporter_name=t.get("reporter_name"),
            labels=t.get("labels") or [],
            created_at=t.get("created_at"),
            updated_at=t.get("updated_at"),
            similarity_score=t.get("similarity_score", 0.0),
        )
        for t in result["tickets"]
    ]
    log.info(
        "Similar tickets: method=%s found=%d",
        result["search_method"], len(tickets),
    )
    return SimilarTicketsResponse(
        query_summary=result["query_summary"],
        total_found=result["total_found"],
        search_method=result["search_method"],
        tickets=tickets,
    )


@router.post("/analyze-ticket/regression", response_model=TestCaseRegressionResponse)
def find_testcase_regressions(
    request: TestCaseRegressionRequest,
) -> TestCaseRegressionResponse:
    """Flag a new incoming ticket as a possible regression against an existing test case."""
    log.info(
        "POST /analyze-ticket/regression summary=%r project=%s",
        request.summary[:60], request.project_key,
    )
    try:
        finder = TestCaseRegressionFinder(settings=settings)
        result = finder.find_regressions(
            request.summary,
            request.description,
            project_key=request.project_key,
        )
    except Exception as exc:
        log.exception("Test-case regression search failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    matches = [
        TestCaseRegressionMatch(
            jira_ticket_id=m.get("jira_ticket_id", ""),
            project_key=m.get("project_key", ""),
            phase=m.get("phase", "qa"),
            tc_index=m.get("tc_index"),
            title=m.get("title", ""),
            steps=m.get("steps") or [],
            expected=m.get("expected"),
            status=m.get("status", ""),
            ticket_summary=m.get("ticket_summary"),
            ticket_status=m.get("ticket_status"),
            similarity_score=m.get("similarity_score", 0.0),
        )
        for m in result["matches"]
    ]
    log.info(
        "Test-case regression: method=%s found=%d",
        result["search_method"], len(matches),
    )
@router.get("/jira/create-context")
def get_jira_create_context(
    _user: CurrentUser = Depends(require_tab("jira")),
) -> dict[str, Any]:
    """Provides connected repositories, available projects, and user list for ticket creation."""
    from app.jira_client import JiraClient
    from app.rca import repos as rca_repos

    jc = JiraClient(settings)
    connected_repos = rca_repos.list_repos(settings)

    projects: list[dict[str, Any]] = []
    users: list[dict[str, Any]] = []

    if jc.is_configured():
        try:
            p_data = jc._request("GET", "/rest/api/3/project")
            if isinstance(p_data, list):
                projects = [{"key": p.get("key"), "name": p.get("name")} for p in p_data]
        except Exception as exc:
            log.warning("Could not list Jira projects: %s", exc)

        try:
            u_data = jc._request("GET", "/rest/api/3/users/search", params={"maxResults": 50})
            if isinstance(u_data, list):
                users = [
                    {"accountId": u.get("accountId"), "displayName": u.get("displayName"), "email": u.get("emailAddress")}
                    for u in u_data
                    if u.get("accountType") == "atlassian" and u.get("active")
                ]
        except Exception as exc:
            log.warning("Could not list Jira users: %s", exc)

    return {
        "connected_repositories": connected_repos,
        "projects": projects or [{"key": "SCRUM", "name": "Scrum Project"}],
        "users": users,
        "jira_configured": jc.is_configured(),
    }


@router.post("/jira/create-ticket")
def create_jira_ticket(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("jira")),
) -> dict[str, Any]:
    """Create a new Jira ticket with mandatory field checks and linked repository."""
    from app.jira_client import JiraClient

    project_key = str(payload.get("project_key") or "SCRUM").strip().upper()
    summary = str(payload.get("summary") or "").strip()
    description = str(payload.get("description") or "").strip()
    issue_type = str(payload.get("issue_type") or "Task").strip()
    assignee_id = str(payload.get("assignee_id") or "").strip() or None
    priority_name = str(payload.get("priority") or "Medium").strip()
    github_repo_url = str(payload.get("github_repo_url") or "").strip() or None
    pat = str(payload.get("github_pat") or "").strip() or None

    if not summary:
        raise HTTPException(status_code=400, detail="Summary is required")

    jc = JiraClient(settings)
    if not jc.is_configured():
        raise HTTPException(status_code=400, detail="Jira Cloud is not configured in Settings")

    try:
        created = jc.create_ticket(
            project_key=project_key,
            summary=summary,
            description=description,
            issue_type=issue_type,
            assignee_id=assignee_id,
            priority_name=priority_name,
            github_repo_url=github_repo_url,
        )
        issue_key = created.get("key")

        # Auto-bind repository if specified
        if github_repo_url and issue_key:
            try:
                from app.workflow1_reviewer import Workflow1Reviewer
                reviewer = Workflow1Reviewer(settings=settings, prompt_store=prompt_store)
                reviewer._ensure_repository_connected(github_repo_url, pat)
            except Exception as exc:
                log.warning("Repo auto-clone skipped for %s: %s", github_repo_url, exc)

        return {
            "status": "success",
            "key": issue_key,
            "id": created.get("id"),
            "url": f"{jc.base_url}/browse/{issue_key}" if issue_key else "",
            "summary": summary,
        }
    except Exception as exc:
        log.exception("Ticket creation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to create Jira ticket: {str(exc)}")
