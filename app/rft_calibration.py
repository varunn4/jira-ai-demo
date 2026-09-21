"""Learn this team's estimation bias from completed work.

We can't use a ticket's actual time before it's worked on, but we *can* look at
already-closed tickets that have both an Original Estimate and logged time, and
measure how this team's actuals relate to its estimates. The resulting
"overrun factor" (median actual/estimate) calibrates the WF7 should-have time so
it reflects how the team really performs — not an arbitrary FAANG-level bar.
"""

from __future__ import annotations

import logging
from statistics import median
from typing import Any

from app import jira_fetcher
from app.config import Settings

LOGGER = logging.getLogger(__name__)

_FIELDS = "timetracking,timeoriginalestimate,aggregatetimespent"
# Drop per-ticket ratios outside this range as data-entry noise before taking the median.
_RATIO_CLAMP = (0.1, 10.0)


def _spent_estimate(fields: dict[str, Any]) -> tuple[int, int]:
    tracking = fields.get("timetracking") or {}
    est = int(tracking.get("originalEstimateSeconds") or fields.get("timeoriginalestimate") or 0)
    spent = int(tracking.get("timeSpentSeconds") or fields.get("aggregatetimespent") or 0)
    return spent, est


def team_overrun_factor(settings: Settings) -> dict[str, Any]:
    """Median actual/estimate ratio over recently-closed tickets.

    Returns a dict: {available, factor, samples, lookback_days, median_pct}.
    `factor` is 1.0 (neutral) when there isn't enough history. Never raises.
    """
    result = {
        "available": False,
        "factor": 1.0,
        "samples": 0,
        "lookback_days": settings.rft_estimate_history_lookback_days,
        "median_pct": 0,
    }
    project = (settings.rft_estimate_project_key or "RFT").strip()
    lookback = max(1, settings.rft_estimate_history_lookback_days)
    jql = (
        f'project = "{project}" AND statusCategory = Done '
        f'AND timespent > 0 AND updated >= "-{lookback}d" '
        "ORDER BY updated DESC"
    )

    ratios: list[float] = []
    next_page_token: str | None = None
    start = 0
    pages = 0
    try:
        while pages < 10:  # cap at ~1000 closed tickets — plenty for a median
            params: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": _FIELDS}
            if next_page_token:
                params["nextPageToken"] = next_page_token
            else:
                params["startAt"] = start
            data = jira_fetcher._jira_get("/rest/api/3/search/jql", params)
            batch = data.get("issues", []) or []
            for issue in batch:
                spent, est = _spent_estimate(issue.get("fields", {}) or {})
                if spent > 0 and est > 0:
                    ratio = spent / est
                    if _RATIO_CLAMP[0] <= ratio <= _RATIO_CLAMP[1]:
                        ratios.append(ratio)
            pages += 1
            start += len(batch)
            next_page_token = data.get("nextPageToken")
            if data.get("isLast") is True or not batch:
                break
            if next_page_token:
                continue
            total = data.get("total")
            if total is not None and start >= total:
                break
    except Exception as exc:  # noqa: BLE001 — calibration is best-effort
        LOGGER.warning("rft calibration: history fetch failed (%s); using factor 1.0", exc)
        return result

    result["samples"] = len(ratios)
    if len(ratios) < settings.rft_estimate_history_min_samples:
        LOGGER.info("rft calibration: only %d samples (<%d) — no calibration applied",
                    len(ratios), settings.rft_estimate_history_min_samples)
        return result

    raw = median(ratios)
    factor = max(settings.rft_estimate_factor_min, min(settings.rft_estimate_factor_max, raw))
    result.update(available=True, factor=round(factor, 3), median_pct=round((raw - 1) * 100))
    LOGGER.info("rft calibration: factor=%.2f over %d closed tickets (last %dd)",
                factor, len(ratios), lookback)
    return result


def calibrated_hours(
    llm_hours: float, estimate_seconds: int, factor: float, weight: float
) -> float:
    """Blend the LLM (ticket-aware) estimate with the history projection.

    history projection = Original Estimate × team overrun factor.
    weight 0 → pure LLM, 1 → pure history. Falls back to LLM when no estimate.
    """
    weight = max(0.0, min(1.0, weight))
    if estimate_seconds <= 0 or factor <= 0:
        return llm_hours
    history_hours = (estimate_seconds / 3600.0) * factor
    return round((1.0 - weight) * llm_hours + weight * history_hours, 2)
