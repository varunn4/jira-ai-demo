"""Analytics, Utilization, Channel Health, and RFT router."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from app.app_settings import set_setting
from app.auth import CurrentUser, require_tab
from app.channel_health import ChannelHealthChecker, ChannelHealthError
from app.config import settings
from app.rft_estimates import (
    SPRINT_SETTING_KEY,
    build_rft_estimate_report,
    get_sprint_setting,
    list_rft_sprints,
)
from app.schemas import (
    AlertBatchResponse,
    ChannelHealthCheckRequest,
    N8nMonitorResponse,
    RftSprintSettingRequest,
)
from app.test_case_comparison_report import (
    build_test_case_comparison_report,
    build_test_case_comparison_sheet,
)
from app.utilization import (
    build_metric_drilldown,
    build_utilization_report,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["Analytics & Health"])


def _excluded_jira_projects() -> list[str]:
    return [
        key.strip().upper()
        for chunk in settings.jira_excluded_project_keys.split(",")
        for key in chunk.split()
        if key.strip()
    ]


# ─── Workflow 7: RFT estimates ──────────────────────────────────────────────

@router.post("/workflow/rft-estimates", response_model=AlertBatchResponse)
def workflow_rft_estimates() -> AlertBatchResponse:
    log.info("POST /workflow/rft-estimates")
    try:
        return AlertBatchResponse(**build_rft_estimate_report(settings))
    except RuntimeError as exc:
        log.exception("/workflow/rft-estimates runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow/rft-estimates failed")
        raise HTTPException(status_code=500, detail=f"rft-estimates failed: {exc}") from exc


# WF7 admin controls — read/update the sprint filter + list available sprints.
@router.get("/graph-admin/rft-estimate/settings")
def graph_admin_rft_estimate_settings(
    _user: CurrentUser = Depends(require_tab("workflows")),
) -> dict[str, Any]:
    return get_sprint_setting(settings)


@router.put("/graph-admin/rft-estimate/settings")
def graph_admin_set_rft_estimate_settings(
    request: RftSprintSettingRequest,
    _user: CurrentUser = Depends(require_tab("workflows")),
) -> dict[str, Any]:
    value = (request.value or "").strip()
    if value not in ("open", "all") and not value.isdigit():
        raise HTTPException(
            status_code=400,
            detail="value must be 'open', 'all', or a numeric sprint id",
        )
    try:
        set_setting(settings, SPRINT_SETTING_KEY, value)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    log.info("RFT estimate sprint filter set to %s", value)
    return get_sprint_setting(settings)


@router.get("/graph-admin/rft-estimate/sprints")
def graph_admin_rft_estimate_sprints(
    _user: CurrentUser = Depends(require_tab("workflows")),
) -> dict[str, Any]:
    return list_rft_sprints(settings)


@router.get("/graph-admin/rft-estimate/calibration")
def graph_admin_rft_estimate_calibration(
    _user: CurrentUser = Depends(require_tab("workflows")),
) -> dict[str, Any]:
    """Team estimate→actual overrun factor used to calibrate WF7 should-have times."""
    from app.rft_calibration import team_overrun_factor

    cal = team_overrun_factor(settings)
    cal["history_weight"] = settings.rft_estimate_history_weight
    cal["persona"] = settings.rft_estimate_benchmark_persona
    return cal


@router.get("/graph-admin/n8n/workflows", response_model=N8nMonitorResponse)
def graph_admin_n8n_workflows(
    _user: CurrentUser = Depends(require_tab("workflows")),
) -> N8nMonitorResponse:
    """List n8n workflows and recent execution metrics."""
    from app.n8n_monitor import N8nMonitor, N8nMonitorError

    monitor = N8nMonitor(settings)
    try:
        data = monitor.overview()
        return N8nMonitorResponse(**data)
    except N8nMonitorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/graph-admin/n8n/workflows failed")
        raise HTTPException(status_code=500, detail=f"n8n monitor failed: {exc}") from exc



# ─── Utilization Analytics ──────────────────────────────────────────────────

@router.get("/graph-admin/utilization")
def graph_admin_utilization(
    _user: CurrentUser = Depends(require_tab("utilization")),
) -> dict[str, Any]:
    log.debug("GET /graph-admin/utilization")
    try:
        return build_utilization_report(settings)
    except Exception as exc:
        log.exception("/graph-admin/utilization failed")
        raise HTTPException(status_code=500, detail=f"utilization failed: {exc}") from exc


@router.get("/graph-admin/utilization/tickets")
def graph_admin_utilization_tickets(
    metric: str,
    status: str = "",
    _user: CurrentUser = Depends(require_tab("utilization")),
) -> dict[str, Any]:
    """Drill-down: the tickets (or repos) that make up one utilization stat box."""
    log.debug("GET /graph-admin/utilization/tickets metric=%s status=%s", metric, status)
    try:
        return build_metric_drilldown(settings, metric, status=status)
    except Exception as exc:
        log.exception("/graph-admin/utilization/tickets failed")
        raise HTTPException(status_code=500, detail=f"drilldown failed: {exc}") from exc


# ─── Channel Health ──────────────────────────────────────────────────────────

@router.get("/graph-admin/channel-health/channels")
def graph_admin_channel_health_channels(
    _user: CurrentUser = Depends(require_tab("channels")),
) -> dict[str, Any]:
    """List the known channel IDs and their sources without sending anything."""
    log.debug("GET /graph-admin/channel-health/channels")
    try:
        channels = ChannelHealthChecker(settings=settings).discover()
    except ChannelHealthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/graph-admin/channel-health/channels failed")
        raise HTTPException(status_code=500, detail=f"channel discovery failed: {exc}") from exc
    return {
        "configured": bool(settings.slack_bot_token),
        "count": len(channels),
        "channels": channels,
    }


@router.post("/graph-admin/channel-health/check")
def graph_admin_channel_health_check(
    request: ChannelHealthCheckRequest,
    _user: CurrentUser = Depends(require_tab("channels")),
) -> dict[str, Any]:
    """Probe each channel with a test message and flag the failures."""
    log.info(
        "POST /graph-admin/channel-health/check ids=%s",
        len(request.channel_ids) if request.channel_ids else "all",
    )
    try:
        return ChannelHealthChecker(settings=settings).check(
            channel_ids=request.channel_ids,
            message=request.message,
        )
    except ChannelHealthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/graph-admin/channel-health/check failed")
        raise HTTPException(status_code=500, detail=f"channel health check failed: {exc}") from exc


# ─── Test Case Comparison Report ─────────────────────────────────────────────

@router.get("/graph-admin/test-case-comparison-report")
def graph_admin_test_case_comparison_report(
    project_key: str | None = None,
    limit: int = 500,
    pipeline_limit: int | None = None,
    format: str = "xlsx",
    _user: CurrentUser = Depends(require_tab("insights")),
) -> Response:
    log.debug(
        "GET /graph-admin/test-case-comparison-report project=%s limit=%d pipeline_limit=%s format=%s",
        project_key,
        limit,
        pipeline_limit,
        format,
    )
    try:
        clean_project_key = project_key.strip().upper() if project_key else None
        safe_limit = max(1, min(limit, 2000))
        safe_pipeline_limit = max(0, min(pipeline_limit, 50)) if pipeline_limit is not None else None
        output_format = (format or "xlsx").strip().lower()
        if output_format in {"md", "markdown"}:
            filename, markdown = build_test_case_comparison_report(
                settings,
                project_key=clean_project_key or None,
                limit=safe_limit,
                pipeline_limit=safe_pipeline_limit,
                excluded_project_keys=_excluded_jira_projects(),
            )
            return Response(
                content=markdown,
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        filename, workbook = build_test_case_comparison_sheet(
            settings,
            project_key=clean_project_key or None,
            limit=safe_limit,
            pipeline_limit=safe_pipeline_limit,
            excluded_project_keys=_excluded_jira_projects(),
        )
        return Response(
            content=workbook,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        log.exception("Failed to build test-case comparison report")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
