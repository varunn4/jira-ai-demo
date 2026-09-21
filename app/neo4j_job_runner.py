"""Background runner for Neo4j code-graph build jobs.

Reuses the in-memory GraphJob/job_store used by the Qdrant pipeline so the
existing polling endpoints work unchanged. The build itself is synchronous
(Neo4j driver + tree-sitter), so it runs in a worker thread; progress events
update the job's logs/progress live.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.graph_job import GraphJob
from app.neo4j_graph.builder import build_graph
from app.neo4j_graph.config import GraphBuildConfig

log = logging.getLogger(__name__)


async def run_neo4j_graph_job(
    job: GraphJob,
    *,
    selected_repositories: list[str] | None = None,
    wipe_mode: str = "managed",
    include_code: bool = True,
    pull_latest: bool | None = None,
) -> None:
    job.status = "running"
    log.info("Neo4j graph job %s started (wipe=%s, code=%s, pull=%s)",
             job.job_id, wipe_mode, include_code, pull_latest)

    def progress(event: dict[str, Any]) -> None:
        if "repos_total" in event:
            job.totals["repositories"] = event["repos_total"]
        if "repos_done" in event:
            job.progress["repositories_done"] = event["repos_done"]
        job.logs.append({
            "step": event.get("phase", "neo4j"),
            "level": event.get("level", "info"),
            "message": event.get("message", ""),
            "at": datetime.now(timezone.utc).isoformat(),
        })

    try:
        cfg = GraphBuildConfig.from_settings()
        result = await asyncio.to_thread(
            build_graph,
            cfg=cfg,
            selected_names=selected_repositories,
            wipe_mode=wipe_mode,
            include_code=include_code,
            pull_latest=pull_latest,
            progress=progress,
        )
        # Surface written node/relationship counts in the job totals.
        for key, value in result.get("counts", {}).items():
            job.totals[key] = value
        job.totals["repositories"] = result.get("repositories", job.totals.get("repositories", 0))
        job.progress["repositories_done"] = result.get("repositories", 0)

        # Record a snapshot so the analytics dashboard can show build-over-build trends.
        try:
            from app.config import settings
            from app.neo4j_graph.analytics import graph_analytics
            from app.neo4j_graph.snapshots import save_snapshot

            save_snapshot(settings, graph_analytics(cfg))
        except Exception as snap_exc:  # noqa: BLE001
            log.debug("post-build snapshot skipped: %s", snap_exc)

        job.mark_done()
        log.info("Neo4j graph job %s completed: %s", job.job_id, result.get("counts"))
    except Exception as exc:  # noqa: BLE001
        log.exception("Neo4j graph job %s failed", job.job_id)
        job.mark_failed(str(exc))
