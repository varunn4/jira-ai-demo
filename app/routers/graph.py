"""Graph Administration and Neo4j endpoints router."""

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response

from app.auth import CurrentUser, require_tab
from app.config import settings
from app.graph_job import job_store
from app.graph_job_runner import run_graph_job
from app.neo4j_graph.analytics import graph_analytics
from app.neo4j_graph.builder import select_active_repositories
from app.neo4j_graph.config import GraphBuildConfig
from app.neo4j_job_runner import run_neo4j_graph_job
from app.repository_discovery import discover_graph_repositories
from app.schemas import (
    CodeAnalysisReportRequest,
    GraphAdminTriggerRequest,
    GraphAdminTriggerResponse,
    GraphJobResponse,
    Neo4jBuildRequest,
    Neo4jBuildResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["Graph Admin"])


def _excluded_repositories() -> list[str]:
    return [
        name.strip()
        for name in settings.excluded_repository_names.split(",")
        if name.strip()
    ]


def _selected_repositories(
    repositories: list[dict[str, Any]],
    selected_values: list[str],
) -> list[dict[str, Any]]:
    if not selected_values:
        return repositories
    selected = set(selected_values)
    return [
        repo for repo in repositories
        if repo.get("name") in selected or repo.get("path") in selected
    ]


@router.get("/graph-admin/repositories")
def graph_admin_repositories(
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    log.info("GET /graph-admin/repositories")
    repositories = discover_graph_repositories(settings)
    log.info("Returning %d repositories", len(repositories))
    return {
        "repository_count": len(repositories),
        "repositories": repositories,
        "excluded_repositories": _excluded_repositories(),
    }


@router.post("/graph-admin/trigger", response_model=GraphAdminTriggerResponse)
def graph_admin_trigger(
    request: GraphAdminTriggerRequest,
    background_tasks: BackgroundTasks,
    _user: CurrentUser = Depends(require_tab("repos")),
) -> GraphAdminTriggerResponse:
    log.info(
        "POST /graph-admin/trigger action=%s pull_code=%s fetch_jira=%s include_jira=%s selected_repos=%d",
        request.action,
        request.pull_latest_code,
        request.fetch_latest_jira_tickets,
        request.include_jira_tickets,
        len(request.repositories),
    )
    repositories = discover_graph_repositories(settings)
    selected_repositories = _selected_repositories(repositories, request.repositories)
    if request.action != "jira_tickets_only" and not selected_repositories:
        raise HTTPException(status_code=400, detail="Select at least one repository")

    job = job_store.create(
        action=request.action,
        user_id=getattr(_user, "id", None),
        user_email=getattr(_user, "email", None),
    )
    log.info("Enqueuing background job %s for action=%s (user=%s)", job.job_id, request.action, getattr(_user, "email", "system"))

    background_tasks.add_task(
        run_graph_job,
        job,
        pull_latest_code=request.pull_latest_code,
        fetch_latest_jira=request.fetch_latest_jira_tickets,
        include_jira_in_graph=request.include_jira_tickets,
        build_embeddings=request.build_embeddings,
        embedding_model=request.embedding_model,
        selected_repositories=[repo.get("name", "") for repo in selected_repositories],
    )

    return GraphAdminTriggerResponse(
        job_id=job.job_id,
        action=job.action,
        status=job.status,
        repository_count=len(selected_repositories) if request.action != "jira_tickets_only" else 0,
        excluded_repositories=_excluded_repositories(),
    )


@router.get("/graph-admin/jobs", response_model=list[GraphJobResponse])
def list_graph_jobs(
    limit: int = 10,
    _user: CurrentUser = Depends(require_tab("repos", "logs")),
) -> list[GraphJobResponse]:
    log.debug("GET /graph-admin/jobs limit=%d", limit)
    user_id = getattr(_user, "id", None) if getattr(_user, "role", "") != "admin" else None
    jobs = job_store.list_recent(limit=min(limit, 50), user_id=user_id)
    return [GraphJobResponse(**j.to_dict()) for j in jobs]


@router.get("/graph-admin/jobs/{job_id}", response_model=GraphJobResponse)
def get_graph_job(
    job_id: str,
    _user: CurrentUser = Depends(require_tab("repos", "logs")),
) -> GraphJobResponse:
    log.debug("GET /graph-admin/jobs/%s", job_id)
    job = job_store.get(job_id)
    if job is None:
        log.warning("Job %s not found", job_id)
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return GraphJobResponse(**job.to_dict())


# ─── Neo4j code-graph admin ────────────────────────────────────────────────

@router.get("/graph-admin/neo4j/active-repositories")
def neo4j_active_repositories(
    _user: CurrentUser = Depends(require_tab("neo4j")),
) -> dict[str, Any]:
    """Active (non-stale) repositories eligible for the Neo4j graph build."""
    cfg = GraphBuildConfig.from_settings()
    repos = select_active_repositories(cfg)
    return {
        "repository_count": len(repos),
        "activity_min_score": cfg.activity_min_score,
        "repositories": repos,
    }


@router.get("/graph-admin/neo4j/analytics")
def neo4j_graph_analytics(
    _user: CurrentUser = Depends(require_tab("neo4j")),
) -> dict[str, Any]:
    """Current node/relationship counts and per-repo / per-language breakdowns,
    plus per-metric trends (delta vs the previous snapshot)."""
    log.debug("GET /graph-admin/neo4j/analytics")
    from app.neo4j_graph.snapshots import attach_trends

    analytics = graph_analytics(GraphBuildConfig.from_settings())
    return attach_trends(settings, analytics)


@router.post("/graph-admin/neo4j/build", response_model=Neo4jBuildResponse)
def neo4j_build(
    request: Neo4jBuildRequest,
    background_tasks: BackgroundTasks,
    _user: CurrentUser = Depends(require_tab("neo4j")),
) -> Neo4jBuildResponse:
    """Build/update the Neo4j code graph for active repos in the background."""
    if request.wipe_mode not in ("managed", "all", "none"):
        raise HTTPException(status_code=400, detail="wipe_mode must be managed|all|none")
    if not settings.neo4j_password:
        raise HTTPException(status_code=503, detail="NEO4J_PASSWORD is not configured on the server")

    cfg = GraphBuildConfig.from_settings()
    active = select_active_repositories(cfg, request.repositories or None)
    if not active:
        raise HTTPException(status_code=400, detail="No active repositories matched the request")

    job = job_store.create(action="neo4j_build")
    log.info("Enqueuing Neo4j graph job %s (repos=%d wipe=%s code=%s)",
             job.job_id, len(active), request.wipe_mode, request.include_code)
    background_tasks.add_task(
        run_neo4j_graph_job,
        job,
        selected_repositories=request.repositories or None,
        wipe_mode=request.wipe_mode,
        include_code=request.include_code,
        pull_latest=request.pull_latest,
    )
    return Neo4jBuildResponse(
        job_id=job.job_id, status=job.status, repository_count=len(active)
    )


@router.get("/graph-admin/neo4j/jobs/{job_id}", response_model=GraphJobResponse)
def neo4j_get_job(
    job_id: str,
    _user: CurrentUser = Depends(require_tab("neo4j")),
) -> GraphJobResponse:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return GraphJobResponse(**job.to_dict())


@router.post("/graph-admin/code-analysis-report")
async def download_code_analysis_report(
    request: CodeAnalysisReportRequest,
    _user: CurrentUser = Depends(require_tab("repos")),
) -> Response:
    from app.code_analysis_report import build_code_analysis_report

    selected_repositories = discover_graph_repositories(settings, only_names=request.repositories)
    if not selected_repositories:
        raise HTTPException(status_code=400, detail="Select at least one known repository")

    filename, markdown = await build_code_analysis_report(
        settings=settings,
        selected_repositories=selected_repositories,
        include_graph_context=request.include_graph_context,
        embedding_model=request.embedding_model,
    )

    if request.format == "pdf":
        from app.pdf_generator import markdown_to_pdf
        repo_label = ", ".join(r["name"] for r in selected_repositories[:2])
        if len(selected_repositories) > 2:
            repo_label += f" (+{len(selected_repositories)-2} more)"
        try:
            pdf_bytes = markdown_to_pdf(markdown, repo_name=repo_label)
            pdf_filename = filename.replace(".md", ".pdf")
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{pdf_filename}"'},
            )
        except Exception as exc:
            log.exception("PDF conversion failed, falling back to markdown: %s", exc)

    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


