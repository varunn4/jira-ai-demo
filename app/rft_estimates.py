"""RFT estimate report (Workflow 7).

Finds open RFT tickets that have a Jira **Original Estimate** filled and builds
a single Slack digest for the governor channel, returned in the shared
``AlertBatchResponse`` shape (``{"alerts": [...], "alerts_sent": N}``) that the
WF7 Split-Alerts → Slack nodes already consume.

The Jira ticket cache does not store time-tracking, so this queries Jira live
via the project-scoped ``/rest/api/3/search/jql`` endpoint, requests the
``timetracking`` field, and filters in Python on ``originalEstimateSeconds``
(robust — no reliance on a JQL estimate field name that may differ per
instance). Scope is ``statusCategory != Done``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app import jira_fetcher
from app.app_settings import get_setting

LOGGER = logging.getLogger(__name__)

_FIELDS = "summary,status,issuetype,assignee,priority,parent,timetracking,timeoriginalestimate"

# Admin-editable setting key. Value semantics:
#   "open"  → sprint in openSprints()  (current/active sprint)
#   "<id>"  → sprint = <id>            (a specific sprint, e.g. "284")
#   "all"   → no sprint filter         (whole project)
SPRINT_SETTING_KEY = "rft_estimate_sprint"


def resolve_sprint_value(settings: Settings) -> str:
    """Effective sprint selection: DB setting wins, else env config."""
    stored = get_setting(settings, SPRINT_SETTING_KEY)
    if stored is not None and stored.strip():
        return stored.strip()
    if (settings.rft_estimate_sprint_id or "").strip():
        return settings.rft_estimate_sprint_id.strip()
    return "open" if settings.rft_estimate_current_sprint_only else "all"


def _fmt_estimate(seconds: int, pretty: str) -> str:
    """Prefer Jira's own pretty string ('2d 4h'); else derive from seconds."""
    if pretty:
        return pretty
    if not seconds:
        return ""
    # Jira working day = 8h, working week = 5d (Jira defaults).
    units = [("w", 5 * 8 * 3600), ("d", 8 * 3600), ("h", 3600), ("m", 60)]
    parts: list[str] = []
    remaining = int(seconds)
    for label, size in units:
        if remaining >= size:
            qty, remaining = divmod(remaining, size)
            parts.append(f"{qty}{label}")
    return " ".join(parts) or f"{seconds}s"


def _sprint_clause(settings: Settings) -> str:
    """JQL fragment restricting to the selected sprint, or '' for no restriction."""
    value = resolve_sprint_value(settings)
    if value in ("all", "none", ""):
        return ""
    if value == "open":
        return "sprint in openSprints()"
    if value.isdigit():
        return f"sprint = {value}"
    # Unknown value — be safe and don't filter.
    return ""


def _resolve_project_key(settings: Settings) -> str:
    stored = get_setting(settings, "jira_project_key") or get_setting(settings, "jira_project_keys")
    if stored and stored.strip():
        return stored.strip().split(",")[0].strip()
    user_proj = getattr(settings, "jira_project_key", "") or getattr(settings, "jira_project_keys", "")
    if user_proj and user_proj.strip() and user_proj.strip().upper() != "RFT":
        return user_proj.strip().split(",")[0].strip()
    return (settings.rft_estimate_project_key or "SCRUM").strip()


def _fetch_estimated_tickets(settings: Settings) -> list[dict[str, Any]]:
    project = _resolve_project_key(settings)
    clauses = [f'project = "{project}"', "statusCategory != Done"]
    sprint_clause = _sprint_clause(settings)
    if sprint_clause:
        clauses.append(sprint_clause)
    jql = " AND ".join(clauses) + " ORDER BY created DESC"

    # Skip tickets at or below the minimum estimate (too small to analyze).
    min_seconds = int(round(max(0.0, settings.rft_estimate_min_hours) * 3600))

    tickets: list[dict[str, Any]] = []
    start = 0
    next_page_token: str | None = None

    while True:
        params: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": _FIELDS}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        else:
            params["startAt"] = start

        data = jira_fetcher._jira_get("/rest/api/3/search/jql", params)
        batch = data.get("issues", []) or []

        for issue in batch:
            fields = issue.get("fields", {}) or {}
            tracking = fields.get("timetracking") or {}
            seconds = (
                tracking.get("originalEstimateSeconds")
                or fields.get("timeoriginalestimate")
                or 0
            )
            if not seconds or int(seconds) <= min_seconds:
                continue
            assignee = fields.get("assignee") or {}
            parent = fields.get("parent") or {}
            tickets.append(
                {
                    "key": issue.get("key", ""),
                    "summary": (fields.get("summary") or "").strip(),
                    "status": (fields.get("status") or {}).get("name", ""),
                    "issue_type": (fields.get("issuetype") or {}).get("name", ""),
                    "assignee": assignee.get("displayName") or "Unassigned",
                    "parent_key": parent.get("key") or "",
                    "estimate_seconds": int(seconds),
                    "estimate": _fmt_estimate(
                        int(seconds), (tracking.get("originalEstimate") or "").strip()
                    ),
                }
            )

        start += len(batch)
        next_page_token = data.get("nextPageToken")
        if data.get("isLast") is True:
            break
        if next_page_token:
            continue
        total = data.get("total")
        if total is not None and start >= total:
            break
        if not batch:
            break

    # Largest estimate first.
    tickets.sort(key=lambda t: t["estimate_seconds"], reverse=True)
    return tickets


def _sprint_funnel_counts(settings: Settings) -> dict[str, Any] | None:
    """Count the estimation funnel across the whole sprint scope (all statuses).

    Returns {sprint_total, with_estimate, eligible, min_hours} or None on failure.
    `eligible` = tickets with Original Estimate strictly greater than the 1-day
    minimum (the set the analyzer considers).
    """
    # Scope to the SAME population the report analyzes (open tickets in the
    # sprint) so the funnel chains: total → with estimate → > 1 day → flagged.
    project = _resolve_project_key(settings)
    clauses = [f'project = "{project}"', "statusCategory != Done"]
    sprint_clause = _sprint_clause(settings)
    if sprint_clause:
        clauses.append(sprint_clause)
    jql = " AND ".join(clauses) + " ORDER BY created DESC"
    min_seconds = int(round(max(0.0, settings.rft_estimate_min_hours) * 3600))

    total = with_estimate = eligible = 0
    next_page_token: str | None = None
    start = 0
    pages = 0
    try:
        while pages < 30:  # safety cap (~3000 issues)
            params: dict[str, Any] = {
                "jql": jql, "maxResults": 100,
                "fields": "timetracking,timeoriginalestimate",
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token
            else:
                params["startAt"] = start
            data = jira_fetcher._jira_get("/rest/api/3/search/jql", params)
            batch = data.get("issues", []) or []
            for issue in batch:
                fields = issue.get("fields", {}) or {}
                tracking = fields.get("timetracking") or {}
                est = int(tracking.get("originalEstimateSeconds") or fields.get("timeoriginalestimate") or 0)
                total += 1
                if est > 0:
                    with_estimate += 1
                if est > min_seconds:
                    eligible += 1
            pages += 1
            start += len(batch)
            next_page_token = data.get("nextPageToken")
            if data.get("isLast") is True or not batch:
                break
            if next_page_token:
                continue
            jira_total = data.get("total")
            if jira_total is not None and start >= jira_total:
                break
    except Exception as exc:  # noqa: BLE001 — funnel is informational only
        LOGGER.warning("rft funnel count failed: %s", exc)
        return None
    return {
        "sprint_total": total,
        "with_estimate": with_estimate,
        "eligible": eligible,
        "min_hours": settings.rft_estimate_min_hours,
    }


def _link(settings: Settings, key: str) -> str:
    base = settings.jira_base_url
    return f"<{base}/browse/{key}|{key}>" if base else key


def _scope_label(settings: Settings) -> str:
    value = resolve_sprint_value(settings)
    if value == "open":
        return "open, current sprint"
    if value.isdigit():
        return f"open, sprint {value}"
    return "open"


def _build_message(settings: Settings, tickets: list[dict[str, Any]]) -> str:
    project = _resolve_project_key(settings)
    scope = _scope_label(settings)
    if not tickets:
        return f":bar_chart: *{project} tickets with an Original Estimate ({scope})* — none found."

    cap = max(1, settings.rft_estimate_max_rows)
    shown = tickets[:cap]
    lines = [
        f":bar_chart: *{project} tickets with an Original Estimate ({scope})* — "
        f"{len(tickets)} ticket{'s' if len(tickets) != 1 else ''}",
    ]
    for t in shown:
        lines.append(
            f"• {_link(settings, t['key'])} · {t['summary'][:90]} · "
            f"*{t['estimate']}* · {t['assignee']} · _{t['status']}_"
        )
    if len(tickets) > cap:
        lines.append(f"…and {len(tickets) - cap} more")
    return "\n".join(lines)


def build_rft_estimate_report(settings: Settings) -> dict[str, Any]:
    channel_id = (
        getattr(settings, "rft_estimate_channel_id", "")
        or getattr(settings, "governor_notify_channel_id", "")
        or getattr(settings, "slack_channel_id", "")
        or getattr(settings, "slack_default_channel_id", "")
        or "C04DEMO-ALERTS"
    ).strip()
    if not all([settings.jira_base_url, settings.jira_email, settings.jira_api_token]):
        raise RuntimeError("JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN are required")

    tickets = _fetch_estimated_tickets(settings)
    LOGGER.info("rft-estimates: %d open tickets with an original estimate", len(tickets))

    if not tickets:
        return {
            "alerts": [{"channel_id": channel_id, "message": _build_message(settings, [])}],
            "alerts_sent": 1,
            "ticket_count": 0,
        }

    if settings.rft_estimate_analyze:
        from app.rft_estimate_analysis import analyze_tickets

        stats = analyze_tickets(settings, tickets)
        stats["funnel"] = _sprint_funnel_counts(settings)
        LOGGER.info("rft-estimates: analyzed=%(analyzed)s flagged=%(flagged)s llm=%(llm)s", stats)
        alerts = _build_analysis_alerts(settings, channel_id, tickets, stats)
    else:
        alerts = [{"channel_id": channel_id, "message": _build_message(settings, tickets)}]

    return {
        "alerts": alerts,
        "alerts_sent": len(alerts),
        "ticket_count": len(tickets),
    }


# ── hierarchical Story → Task analysis report (multi-part, chunked) ──────────
_CHAR_BUDGET = 2800


def _fmt_delta(pct: Any) -> str:
    if pct is None:
        return "—"
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


def _is_story(t: dict[str, Any] | None) -> bool:
    return bool(t) and (t.get("issue_type") or "").lower() == "story"


def _fetch_parent_meta(settings: Settings, keys: list[str]) -> dict[str, dict[str, Any]]:
    """Batch lookup of summary + issue type for parent keys not in the set."""
    meta: dict[str, dict[str, Any]] = {}
    chunk = 80
    for i in range(0, len(keys), chunk):
        batch = keys[i : i + chunk]
        jql = "key in (" + ",".join(batch) + ")"
        try:
            data = jira_fetcher._jira_get(
                "/rest/api/3/search/jql",
                {"jql": jql, "maxResults": chunk, "fields": "summary,issuetype"},
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("parent meta fetch failed: %s", exc)
            continue
        for issue in data.get("issues", []) or []:
            fields = issue.get("fields", {}) or {}
            meta[issue.get("key", "")] = {
                "summary": (fields.get("summary") or "").strip(),
                "issue_type": (fields.get("issuetype") or {}).get("name", ""),
            }
    return meta


def _resolve_groups(
    settings: Settings, tickets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group analyzed tickets under their parent Story.

    Returns (story_groups, other_tasks). A group = {key, summary, ticket (the
    Story's own analyzed row or None), children:[...]}. Parent Stories not in
    the set are fetched (summary + type) so they can still head a group.
    """
    by_key = {t["key"]: t for t in tickets}
    missing = sorted(
        {t["parent_key"] for t in tickets if t.get("parent_key") and t["parent_key"] not in by_key}
    )
    parent_meta = _fetch_parent_meta(settings, missing) if missing else {}

    def is_story_key(key: str) -> bool:
        if key in by_key:
            return _is_story(by_key[key])
        meta = parent_meta.get(key)
        return bool(meta and (meta.get("issue_type") or "").lower() == "story")

    def summary_of(key: str) -> str:
        if key in by_key:
            return by_key[key].get("summary", "")
        return (parent_meta.get(key) or {}).get("summary", "")

    groups: dict[str, dict[str, Any]] = {}

    def ensure_group(key: str) -> dict[str, Any]:
        if key not in groups:
            groups[key] = {
                "key": key,
                "summary": summary_of(key),
                "ticket": by_key.get(key),
                "children": [],
            }
        return groups[key]

    other: list[dict[str, Any]] = []
    for t in tickets:
        if _is_story(t):
            ensure_group(t["key"])
            continue
        pkey = t.get("parent_key")
        if pkey and is_story_key(pkey):
            ensure_group(pkey)["children"].append(t)
        else:
            other.append(t)

    def _est(t: dict[str, Any] | None) -> int:
        return int(t["estimate_seconds"]) if t else 0

    ordered = sorted(
        groups.values(),
        key=lambda g: (_est(g["ticket"]), len(g["children"])),
        reverse=True,
    )
    for g in ordered:
        g["children"].sort(key=lambda c: _est(c), reverse=True)
    other.sort(key=lambda t: _est(t), reverse=True)
    return ordered, other


def _render_item(label: str, t: dict[str, Any] | None, indent: str) -> str:
    """'<label>: [FLAG · Δ% · low/med reason] explanation' (or header if no analysis)."""
    flag = (t or {}).get("flag")
    if t is None or not flag or flag == "n/a":
        base = f"{indent}{label}"
        if t is not None and flag == "n/a":
            return f"{base}: _not analyzed yet_"
        return f"{base}:"
    seg = [flag, _fmt_delta(t.get("delta_pct"))]
    predicted = t.get("predicted")
    if predicted and predicted != "—":
        est = t.get("estimate") or _fmt_estimate(int(t.get("estimate_seconds", 0)), "")
        seg.append(f"should {predicted} vs est {est}")
    conf = (t.get("confidence") or "").lower()
    if conf in ("low", "medium") and t.get("reason"):
        seg.append(f"{conf} conf: {t['reason']}")
    explanation = (t.get("explanation") or "").strip()
    tail = f" {explanation}" if explanation else ""
    return f"{indent}{label}: [{' · '.join(seg)}]{tail}"


def _child_label(t: dict[str, Any]) -> str:
    label = f"↳ *{t['key']}*"
    if t.get("summary"):
        label += f" — {t['summary'][:60]}"
    return label


def _render_report_lines(
    groups: list[dict[str, Any]], other: list[dict[str, Any]]
) -> list[str]:
    # Hide tickets within tolerance ('OK'); keep UNDER / PLUS / n/a. A Story is
    # kept as a header (for context) when it has visible children even if its
    # own line is OK; an all-OK group is dropped entirely.
    lines: list[str] = []
    for g in groups:
        story = g.get("ticket")
        story_visible = bool(story) and story.get("flag") != "OK"
        visible_children = [c for c in g["children"] if c.get("flag") != "OK"]
        if not story_visible and not visible_children:
            continue

        title = f"*{g['key']}*"
        if g.get("summary"):
            title += f" — {g['summary'][:70]}"
        # Show the Story's own analysis only when it is itself flagged; otherwise
        # render a plain header so it just groups its flagged children.
        lines.append(_render_item(title, story if story_visible else None, ""))
        for child in visible_children:
            lines.append(_render_item(_child_label(child), child, "   "))
        lines.append("")  # spacer between stories

    visible_other = [t for t in other if t.get("flag") != "OK"]
    if visible_other:
        lines.append("*Other tasks (Not In Any Story)*")
        for t in visible_other:
            lines.append(_render_item(_child_label(t), t, "   "))
    return lines


def _chunk_lines(lines: list[str]) -> list[list[str]]:
    """Split rendered lines into Slack-sized parts, repeating the section header
    as '(cont.)' context when a split lands mid-section."""
    parts: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    header: str | None = None  # most recent non-indented bold header line
    for line in lines:
        if line and not line.startswith(" ") and line.startswith("*"):
            header = line.split("  _(cont.)_")[0]
        if cur and cur_len + len(line) + 1 > _CHAR_BUDGET:
            parts.append(cur)
            cur = []
            cur_len = 0
            if line.startswith(" ") and header:  # mid-section child after a split
                ctx = f"{header}  _(cont.)_"
                cur.append(ctx)
                cur_len += len(ctx) + 1
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        parts.append(cur)
    return parts or [[]]


# Drift buckets for the summary matrix: |predicted − Original Estimate| %.
_DRIFT_BUCKETS = (("<25%", 0, 25), ("25-50%", 25, 50), ("50-100%", 50, 100), ("100%+", 100, float("inf")))


def _drift_bucket(abs_pct: float) -> int:
    for i, (_label, lo, hi) in enumerate(_DRIFT_BUCKETS):
        if lo <= abs_pct < hi:
            return i
    return len(_DRIFT_BUCKETS) - 1


class _Cell:
    """Accumulates count + total Original Estimate + total predicted time (seconds)."""

    __slots__ = ("count", "est_s", "pred_s")

    def __init__(self) -> None:
        self.count = 0
        self.est_s = 0
        self.pred_s = 0

    def add(self, est_s: int, pred_s: int) -> None:
        self.count += 1
        self.est_s += est_s
        self.pred_s += pred_s

    def merge(self, other: "_Cell") -> None:
        self.count += other.count
        self.est_s += other.est_s
        self.pred_s += other.pred_s

    def render(self) -> str:
        if self.count == 0:
            return "0"
        return f"{self.count} ({_fmt_hours_only(self.est_s)} → {_fmt_hours_only(self.pred_s)})"


def _fmt_hours_only(seconds: int) -> str:
    """Whole-or-one-decimal hours, e.g. '40h' or '27.5h'."""
    hours = (seconds or 0) / 3600.0
    return f"{hours:.0f}h" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f}h"


def _build_summary_matrix(tickets: list[dict[str, Any]]) -> str:
    """Over/Under × drift-magnitude matrix over all analyzed tickets.

    Over  = developer over-estimated (should-have time < Original Estimate)
    Under = developer under-estimated (should-have time > Original Estimate)
    Each cell: count (Σ Original Estimate → Σ should-have time).
    """
    over = [_Cell() for _ in _DRIFT_BUCKETS]
    under = [_Cell() for _ in _DRIFT_BUCKETS]
    any_row = False
    for t in tickets:
        d = t.get("delta_pct")
        if d is None or t.get("flag") in (None, "n/a"):
            continue
        est_s = int(t.get("estimate_seconds") or 0)
        ph = t.get("predicted_hours")
        pred_s = int(round(float(ph) * 3600)) if ph else 0
        idx = _drift_bucket(abs(d))
        if d < 0:
            over[idx].add(est_s, pred_s)
            any_row = True
        elif d > 0:
            under[idx].add(est_s, pred_s)
            any_row = True
    if not any_row:
        return ""

    def _row_total(cells: list[_Cell]) -> _Cell:
        agg = _Cell()
        for c in cells:
            agg.merge(c)
        return agg

    col_totals = []
    for i in range(len(_DRIFT_BUCKETS)):
        c = _Cell()
        c.merge(over[i])
        c.merge(under[i])
        col_totals.append(c)
    grand = _row_total(col_totals)

    cols = [b[0] for b in _DRIFT_BUCKETS]
    header = ["", *cols, "Total"]
    body = [
        ["Over", *(c.render() for c in over), _row_total(over).render()],
        ["Under", *(c.render() for c in under), _row_total(under).render()],
        ["Total", *(c.render() for c in col_totals), grand.render()],
    ]
    widths = [max(len(header[c]), *(len(r[c]) for r in body)) for c in range(len(header))]

    def _row(cells: list[str]) -> str:
        out = [cells[0].ljust(widths[0])]
        out += [cells[c].rjust(widths[c]) for c in range(1, len(widths))]
        return "  ".join(out)

    table = "\n".join([_row(header), *(_row(r) for r in body)])
    return (
        "\n\n*Estimate drift summary* "
        "(Over = developer over-estimated, Under = under-estimated; "
        "% = how far the should-have time is from the Original Estimate). "
        "Each cell: count (total Original Estimate → total should-have time):\n"
        f"```\n{table}\n```"
    )


def _funnel_block(stats: dict[str, Any], tickets: list[dict[str, Any]]) -> str:
    """Estimation funnel (open tickets): total → with estimate → > 1 day →
    analyzed → flagged. All counts are open-ticket scoped so they chain."""
    over = sum(1 for t in tickets if t.get("flag") == "PLUS")   # over-estimated
    under = sum(1 for t in tickets if t.get("flag") == "UNDER")  # under-estimated
    analyzed = [t for t in tickets if t.get("flag") not in (None, "n/a")]
    on_target = sum(1 for t in analyzed if (t.get("delta_pct") or 0) == 0)
    over_under = len(analyzed) - on_target  # == drift-table Total
    lines = ["\n*Sprint estimation funnel (open tickets):*"]
    f = stats.get("funnel")
    if f:
        lines.append(f"• Total tickets in this sprint: *{f['sprint_total']}*")
        lines.append(f"• With an estimate (> 0): *{f['with_estimate']}*")
        lines.append(f"• With an estimate > 1 day: *{f['eligible']}*  ← analyzed pool")
    lines.append(
        f"• Analyzed by the model: *{len(analyzed)}* "
        f"({over_under} over/under, {on_target} on-target)"
    )
    lines.append(
        f"• Flagged outside ±tolerance: *{over + under}* "
        f"({over} over-estimated · {under} under-estimated)"
    )
    return "\n".join(lines)


def _calibration_note(stats: dict[str, Any]) -> str:
    """One-line note on how the should-have time was calibrated to the team."""
    cal = stats.get("calibration") or {}
    if not cal.get("available"):
        return (
            "\n_Calibration: not enough closed-ticket history yet — using the "
            "model estimate as-is._"
        )
    pct = cal.get("median_pct", 0)
    direction = "over" if pct >= 0 else "under"
    return (
        f"\n_Calibrated to this team: closed tickets historically run "
        f"{abs(pct)}% {direction} their estimate (factor ×{cal.get('factor')}, "
        f"n={cal.get('samples')}, last {cal.get('lookback_days')}d), blended into "
        f"the should-have time._"
    )


def _build_overbudget_breakdown(tickets: list[dict[str, Any]]) -> str:
    """Who / what kind of work drives the over-budget (under-estimated) tickets.

    Over-budget = should-have time > Original Estimate (delta_pct > 0). For each
    assignee and issue type we sum the shortfall (should-have − estimate) hours.
    """
    by_assignee: dict[str, dict[str, float]] = {}
    by_type: dict[str, dict[str, float]] = {}
    for t in tickets:
        d = t.get("delta_pct")
        if d is None or d <= 0 or t.get("flag") in (None, "n/a"):
            continue
        ph = t.get("predicted_hours")
        if ph is None:
            continue
        gap_h = max(0.0, float(ph) - int(t.get("estimate_seconds", 0)) / 3600.0)
        for bucket, key in (
            (by_assignee, t.get("assignee") or "Unassigned"),
            (by_type, t.get("issue_type") or "—"),
        ):
            agg = bucket.setdefault(key, {"count": 0, "gap": 0.0})
            agg["count"] += 1
            agg["gap"] += gap_h

    if not by_assignee:
        return ""

    def _fmt(bucket: dict[str, dict[str, float]], top: int = 6) -> str:
        rows = sorted(bucket.items(), key=lambda kv: kv[1]["gap"], reverse=True)[:top]
        return ", ".join(f"{name} {int(v['count'])} (+{v['gap']:.0f}h)" for name, v in rows)

    return (
        "\n\n*Over-budget drivers* (under-estimated tickets, +hours = shortfall vs estimate):"
        f"\n• By assignee: {_fmt(by_assignee)}"
        f"\n• By type: {_fmt(by_type)}"
    )


def _build_analysis_alerts(
    settings: Settings,
    channel_id: str,
    tickets: list[dict[str, Any]],
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    project = _resolve_project_key(settings)
    scope = _scope_label(settings)

    groups, other = _resolve_groups(settings, tickets)
    report_lines = _render_report_lines(groups, other)
    summary_matrix = _build_summary_matrix(tickets) + _build_overbudget_breakdown(tickets)

    base_headline = (
        f":bar_chart: *{project} Estimate Analysis ({scope})* — "
        f"{len(tickets)} ticket{'s' if len(tickets) != 1 else ''} · "
        f"*{stats.get('flagged', 0)} flagged*"
    )
    funnel = _funnel_block(stats, tickets)
    # The funnel + drift matrix + drivers form one "summary" block shown at the
    # very end of the report (the last Slack message), next to the drift table.
    summary_block = funnel + summary_matrix

    # Everything within tolerance — nothing flagged to show, but still report the
    # funnel + drift summary so the distribution is always visible.
    if not report_lines:
        message = (
            f"{base_headline}\n\n_All analyzed tickets are within the "
            "estimate tolerance — nothing to flag._"
        )
        if not stats.get("llm", True):
            message += "\n_⚠ LLM unavailable this run — predictions skipped._"
        message += summary_block
        return [{"channel_id": channel_id, "message": message}]

    parts = _chunk_lines(report_lines)
    total = len(parts)

    alerts: list[dict[str, Any]] = []
    for idx, part in enumerate(parts, 1):
        suffix = f"  (part {idx}/{total})" if total > 1 else ""
        body = "\n".join(part).rstrip()
        message = f"{base_headline}{suffix}\n\n{body}"
        if idx == 1:
            message += (
                "\n\n_Format: [UNDER/PLUS · Δ% · low/med-conf reason] explanation. "
                "UNDER = the Original Estimate looks too low, PLUS = too high. "
                "Within-tolerance tickets are hidden. Predicted = realistic "
                "'should-have' time for an average developer on this team._"
            )
            message += _calibration_note(stats)
            if not stats.get("llm", True):
                message += "\n_⚠ LLM unavailable this run — predictions skipped._"
        # Funnel + drift summary at the very end (last message), by the drift table.
        if idx == total:
            message += summary_block
        alerts.append({"channel_id": channel_id, "message": message})

    return alerts


# ── admin: current sprint selection + available sprints ──────────────────────
def get_sprint_setting(settings: Settings) -> dict[str, Any]:
    """Current sprint selection for the admin UI."""
    value = resolve_sprint_value(settings)
    return {
        "value": value,
        "scope_label": _scope_label(settings),
        "jql_clause": _sprint_clause(settings),
        "project_key": _resolve_project_key(settings),
    }


def list_rft_sprints(settings: Settings) -> dict[str, Any]:
    """Active + future sprints across the project's boards (Jira Agile API).

    Always returns the synthetic 'current' and 'all' choices; the concrete
    sprint list degrades to empty (with an error note) if the Agile API or
    boards are unavailable, so the dropdown still offers manual id entry.
    """
    project = _resolve_project_key(settings)
    options: list[dict[str, Any]] = [
        {"value": "open", "label": "Current sprint (active / open)"},
    ]
    error: str | None = None
    if not all([settings.jira_base_url, settings.jira_email, settings.jira_api_token]):
        error = "Jira is not configured"
    else:
        try:
            seen: set[str] = set()
            for board in _fetch_boards(project):
                board_id = board.get("id")
                board_name = board.get("name", "")
                if board_id is None:
                    continue
                for sprint in _fetch_board_sprints(board_id):
                    sid = str(sprint.get("id"))
                    if sid in seen:
                        continue
                    seen.add(sid)
                    state = sprint.get("state", "")
                    options.append(
                        {
                            "value": sid,
                            "label": f"{sprint.get('name', sid)} · {state} · {board_name}",
                            "state": state,
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("list_rft_sprints failed: %s", exc)
            error = str(exc)

    options.append({"value": "all", "label": "All sprints (no filter)"})
    return {"project_key": project, "options": options, "error": error}


def _fetch_boards(project: str) -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    start = 0

    # Try project-scoped board fetch first
    try:
        while True:
            data = jira_fetcher._jira_get(
                "/rest/agile/1.0/board",
                {"projectKeyOrId": project, "maxResults": 50, "startAt": start},
            )
            values = data.get("values", []) or []
            boards.extend(values)
            if data.get("isLast", True) or not values:
                break
            start += len(values)
        if boards:
            return boards
    except Exception as exc:
        LOGGER.debug("project-scoped board fetch (%s) failed (%s), falling back to all boards", project, exc)

    # Fallback: fetch all accessible boards across the instance
    start = 0
    try:
        while True:
            data = jira_fetcher._jira_get(
                "/rest/agile/1.0/board",
                {"maxResults": 50, "startAt": start},
            )
            values = data.get("values", []) or []
            boards.extend(values)
            if data.get("isLast", True) or not values:
                break
            start += len(values)
    except Exception as exc:
        LOGGER.warning("fallback all-boards fetch failed: %s", exc)

    return boards


def _fetch_board_sprints(board_id: Any) -> list[dict[str, Any]]:
    sprints: list[dict[str, Any]] = []
    start = 0
    while True:
        try:
            data = jira_fetcher._jira_get(
                f"/rest/agile/1.0/board/{board_id}/sprint",
                {"state": "active,future", "maxResults": 50, "startAt": start},
            )
        except Exception as exc:  # noqa: BLE001 - some boards (kanban) have no sprints
            LOGGER.debug("board %s sprint fetch skipped: %s", board_id, exc)
            break
        values = data.get("values", []) or []
        sprints.extend(values)
        if data.get("isLast", True) or not values:
            break
        start += len(values)
    return sprints
