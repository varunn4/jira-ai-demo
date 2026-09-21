"""System, health, prompts, and settings router."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.auth import CurrentUser, require_tab
from app.config import settings
from app.embedding_status import get_embeddings_status
from app.graph_job import job_store
from app.prompt_store import PromptStore
from app.repo_tree_integration import repo_tree_status
from app.schemas import PromptListResponse, RepomixReindexRequest, RepomixReindexResponse
from app.testcase_embeddings import run_testcase_embedding_job

log = logging.getLogger(__name__)

router = APIRouter(tags=["System"])
prompt_store = PromptStore(settings.prompt_dir)


@router.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "model": settings.llm_model,
        "repo_tree": repo_tree_status(),
    }


@router.get("/prompts", response_model=PromptListResponse)
def list_prompts() -> PromptListResponse:
    return PromptListResponse(prompts=prompt_store.list_prompts())


@router.get("/graph-admin/embeddings/status")
def embeddings_status(
    _user: CurrentUser = Depends(require_tab("repos", "logs", "rca")),
) -> dict[str, Any]:
    """Live health, point counts, and status for embedding collections."""
    return get_embeddings_status(settings, job_store)


@router.post("/graph-admin/testcase-embeddings/build")
def build_testcase_embeddings_endpoint(
    background_tasks: BackgroundTasks,
    _user: CurrentUser = Depends(require_tab("repos", "testcases")),
) -> dict[str, Any]:
    """Embed all generated test cases into the test_cases Qdrant collection."""
    for existing in job_store.list_recent(limit=10):
        if existing.action == "testcase_embeddings" and existing.status in ("pending", "running"):
            return {"job_id": existing.job_id, "status": existing.status, "already_running": True}

    job = job_store.create(action="testcase_embeddings")
    log.info("Enqueuing test-case embedding job %s", job.job_id)
    background_tasks.add_task(run_testcase_embedding_job, job, settings)
    return {"job_id": job.job_id, "status": job.status, "already_running": False}


@router.post("/graph-admin/repomix/reindex", response_model=RepomixReindexResponse)
async def repomix_reindex(
    request: RepomixReindexRequest,
    _user: CurrentUser = Depends(require_tab("repos", "logs")),
) -> RepomixReindexResponse:
    """Pack or re-pack repositories into XML using RepoMix."""
    from pathlib import Path
    try:
        from repo_architect.config import Config, RepoConfig, load_config
        from repo_architect.reindex import reindex_repomix
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"RepoMix module not available: {exc}") from exc

    try:
        config = load_config(settings.repo_tree_config_path)
    except Exception:
        config = Config()
        config.ensure_dirs()

    known_names = {r.name for r in config.repos}

    # Dynamically register any selected repository found in search root
    search_root = Path(settings.repository_search_root).expanduser().resolve()
    for name in request.repositories:
        if name not in known_names:
            repo_path = search_root / name
            if repo_path.exists() and (repo_path / ".git").exists():
                r_cfg = RepoConfig(name=name, path=repo_path, repo_root=repo_path)
                config.repos.append(r_cfg)
                known_names.add(name)

    selected = [name for name in request.repositories if name in known_names]
    unknown = [name for name in request.repositories if name not in known_names]

    if not selected:
        raise HTTPException(
            status_code=400,
            detail=(
                "No valid repositories found for RepoMix packing. "
                f"Available repos: {', '.join(sorted(known_names)) or 'none'}"
            ),
        )

    try:
        summary = await asyncio.to_thread(
            reindex_repomix,
            config,
            repo_names=selected,
            do_git_pull=request.pull_latest_code,
            force=request.force,
        )
    except Exception as exc:
        log.exception("RepoMix reindex failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RepomixReindexResponse(
        selected=summary.get("selected", selected),
        packed=summary.get("packed", []),
        skipped=summary.get("skipped", []),
        failed=summary.get("failed", []),
        unknown=unknown,
        details=summary.get("details", []),
    )
