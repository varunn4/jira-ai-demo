"""Enriched Workflow4 due-date batches (production + AIGOV test).

Both the production 09:00/15:00 batches and the AIGOV sandbox variants share one
enrichment pipeline (:class:`_Workflow4SummaryMixin`): before the digest is sent
they refetch tickets, rebuild dense embeddings (best-effort), map each Story to
its child issues (assignee + dev/QA due dates), and emit a deterministic tabular
report to the Jira Owner + TL channels — Stories banded by their own due date
(Overdue / 75% / 50% / 25% time left), plus a section for independent tasks.

  * Production (:class:`Workflow4SummaryAssigneeChecker` / ``…TLChecker``) runs
    across all spaces — story scope follows ``STORY_SUBTASK_PROJECT_KEYS``
    (blank = all visible projects minus ``JIRA_EXCLUDED_PROJECT_KEYS``).
  * AIGOV (:class:`Workflow4AigovAssigneeChecker` / ``…TLChecker``) is restricted
    to the AIGOV sandbox and additionally auto-enrolls fetched tickets into
    ``due_date_tracking`` (production relies on workflow1/workflow3 for that).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import requests

from app.workflow4_due_date import (
    DONE_STATUSES,
    Workflow4AssigneeChecker,
    Workflow4TLChecker,
)

LOGGER = logging.getLogger(__name__)

AIGOV_PROJECT_KEY = "AIGOV"

# Per-table row cap so a large project can't blow the Slack message size limit.
_MAX_TABLE_ROWS = 40

# Story time-left bands, rendered in this order (label shown in the message).
_BAND_ORDER = [
    ("overdue", "a. Overdue"),
    ("75", "b. 75% time left"),
    ("50", "c. 50% time left"),
    ("25", "d. 25% time left"),
]

# due_date_tracking.priority is varchar(10) and production stores P0–P4 codes,
# so map the Jira priority *name* down to a code (empty when unknown).
_PRIORITY_NAME_TO_CODE = {
    "highest": "P0",
    "critical": "P0",
    "blocker": "P0",
    "high": "P1",
    "medium": "P2",
    "low": "P3",
    "lowest": "P4",
}


def _normalize_priority(raw: Any) -> str:
    """Coerce a Jira priority (name or code) into a P0–P4 code, capped to 10 chars."""
    value = str(raw or "").strip()
    if not value:
        return ""
    upper = value.upper()
    if upper in {"P0", "P1", "P2", "P3", "P4"}:
        return upper
    return _PRIORITY_NAME_TO_CODE.get(value.lower(), "")[:10]


# ── embeddings (best-effort) ─────────────────────────────────────────────────
def _embed_tickets(tickets: list[dict[str, Any]]) -> None:
    from app.config import settings as global_settings
    from app.graph_job_runner import _ticket_embed_texts
    from app.ollama_embedder import OllamaEmbedder
    from app.qdrant_store import upsert_jira_embeddings

    if not global_settings.qdrant_url:
        LOGGER.info("workflow4-summary: QDRANT_URL not set; skipping embeddings")
        return

    embedder = OllamaEmbedder(
        global_settings.ollama_url,
        global_settings.ollama_embed_model,
        timeout_seconds=global_settings.ollama_embed_timeout_seconds,
        batch_size=global_settings.ollama_embed_batch_size,
        concurrency=global_settings.ollama_embed_concurrency,
    )
    if not embedder.is_available():
        LOGGER.warning("workflow4-summary: Ollama unavailable; skipping embeddings")
        return

    texts = _ticket_embed_texts(tickets)
    embeddings = embedder.embed_batch(texts)
    stored = upsert_jira_embeddings(
        qdrant_url=global_settings.qdrant_url,
        tickets=tickets,
        embeddings=embeddings,
        api_key=global_settings.qdrant_api_key or None,
    )
    LOGGER.info("workflow4-summary: stored %d embedding(s) in Qdrant", stored)


# ── shared enrichment: refetch + embed + map + LLM summary ───────────────────
class _Workflow4SummaryMixin:
    """Refetch tickets, rebuild embeddings, map Story→subtasks, and summarize the
    digest through the LLM. Production scope by default; subclasses override
    ``_fetch_tickets`` / hooks to narrow it."""

    # Production honours the cache TTL to avoid hammering Jira twice a day;
    # the AIGOV test flow forces a refresh for immediacy.
    summary_force_refresh: bool = False
    # Workflow4 is self-sufficient: it enrolls its own fetched tickets into
    # due_date_tracking rather than depending on workflow1/workflow3.
    auto_enroll: bool = True

    # Phase data ({key: {status, done, dev_due, qa_due, live_due, system_due}})
    # prefetched once per run via one JQL so enrollment + the scan don't make a
    # Jira call per ticket. None until _pre_check builds it.
    _phase_cache: dict[str, dict[str, Any]] | None = None

    def _build_embeddings_enabled(self) -> bool:
        """Embeddings are off by default in production (unused by the digest,
        and embedding every ticket per run blows the request budget). Toggle
        via WORKFLOW4_BUILD_EMBEDDINGS."""
        return bool(getattr(self.settings, "workflow4_build_embeddings", False))

    def _scope_jql(self) -> str:
        """JQL project clause for this checker's scope (empty = all projects)."""
        include = self._included_project_keys()
        if include:
            return "project in ({})".format(",".join(f'"{k}"' for k in include))
        excluded = self._excluded_project_keys()
        if excluded:
            return "project not in ({})".format(",".join(f'"{k}"' for k in excluded))
        return ""

    def _build_phase_cache(self) -> dict[str, dict[str, Any]]:
        """One paginated JQL over the scope (non-Done issues) returning each
        ticket's phase due dates — replaces per-ticket `_fetch_jira_phase` calls."""
        from app.jira_fetcher import _jira_get

        s = self.settings
        field_ids = ["status", "duedate", "created", "priority", "issuetype", "assignee"]
        for fid in (
            s.jira_dev_due_date_field,
            s.jira_qa_due_date_field,
            s.jira_live_due_date_field,
        ):
            if fid:
                field_ids.append(fid)

        scope = self._scope_jql()
        jql = (f"{scope} AND " if scope else "") + "statusCategory != Done ORDER BY updated DESC"

        cache: dict[str, dict[str, Any]] = {}
        start = 0
        next_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": 100,
                "fields": ",".join(field_ids),
            }
            if next_token:
                params["nextPageToken"] = next_token
            else:
                params["startAt"] = start

            data = _jira_get("/rest/api/3/search/jql", params)
            batch = data.get("issues", [])
            for issue in batch:
                key = str(issue.get("key") or "")
                if not key:
                    continue
                f = issue.get("fields", {}) or {}
                status = ((f.get("status") or {}).get("name")) or ""
                cache[key] = {
                    "status": status,
                    "done": status in DONE_STATUSES,
                    "dev_due": self._parse_jira_date(f.get(s.jira_dev_due_date_field))
                    if s.jira_dev_due_date_field else None,
                    "qa_due": self._parse_jira_date(f.get(s.jira_qa_due_date_field))
                    if s.jira_qa_due_date_field else None,
                    "live_due": self._parse_jira_date(f.get(s.jira_live_due_date_field))
                    if s.jira_live_due_date_field else None,
                    "system_due": self._parse_jira_date(f.get("duedate")),
                    "created": self._parse_jira_date(f.get("created")),
                    "priority": (f.get("priority") or {}).get("name"),
                    "issuetype": (f.get("issuetype") or {}).get("name"),
                    "assignee_name": (f.get("assignee") or {}).get("displayName"),
                    "assignee_email": (f.get("assignee") or {}).get("emailAddress"),
                }
            start += len(batch)
            next_token = data.get("nextPageToken")
            if data.get("isLast") is True:
                break
            if next_token:
                continue
            total = data.get("total")
            if total is not None and start >= total:
                break
            if not batch:
                break

        LOGGER.info("workflow4-summary: prefetched phase data for %d ticket(s)", len(cache))
        return cache

    _DONE_PHASE = {
        "status": "",
        "done": True,
        "dev_due": None,
        "qa_due": None,
        "live_due": None,
        "system_due": None,
    }

    def _fetch_jira_phase(self, jira_ticket_id: str) -> dict[str, Any]:
        # Serve from the per-run bulk cache; fall back to a live call only for
        # keys not covered (e.g. an already-enrolled ticket that is now Done or
        # was deleted). A 404 means the issue is gone → treat as Done so the
        # scan completes/skips it instead of erroring.
        cache = getattr(self, "_phase_cache", None)
        if cache and jira_ticket_id in cache:
            return cache[jira_ticket_id]
        try:
            return super()._fetch_jira_phase(jira_ticket_id)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                LOGGER.info(
                    "%s: %s not found in Jira (404); treating as Done",
                    self.name,
                    jira_ticket_id,
                )
                return dict(self._DONE_PHASE)
            raise

    def _fetch_tickets(self) -> list[dict[str, Any]]:
        from app.jira_fetcher import fetch_all_tickets, fetch_project_tickets

        include = self._included_project_keys()
        if include:
            tickets: list[dict[str, Any]] = []
            for key in include:
                tickets.extend(
                    fetch_project_tickets(key, force_refresh=self.summary_force_refresh)
                )
            return tickets
        return fetch_all_tickets(force_refresh=self.summary_force_refresh)

    def _mapping_project_keys(self) -> list[str] | None:
        """Story-mapping scope for this checker. Production honours
        WORKFLOW4_PROJECT_KEYS; None lets map_and_store fall back to
        STORY_SUBTASK_PROJECT_KEYS / all-spaces."""
        return self._included_project_keys() or None

    def _pre_check(self) -> None:
        try:
            self._phase_cache = self._build_phase_cache()
        except Exception:
            LOGGER.exception("workflow4-summary: phase prefetch failed (non-fatal)")
            self._phase_cache = {}

        # The mirror fetch + embeddings are only needed when embeddings are on;
        # enrollment and the scan both read the phase cache, not the mirror.
        if self._build_embeddings_enabled():
            try:
                tickets = self._fetch_tickets()
                if tickets:
                    _embed_tickets(tickets)
            except Exception:
                LOGGER.exception("workflow4-summary: refetch/embed failed (non-fatal)")

        if self.auto_enroll:
            try:
                self._enroll_tickets()
            except Exception:
                LOGGER.exception("%s: enrollment step failed (non-fatal)", self.name)

        try:
            from app.story_subtasks import map_and_store

            map_and_store(self.settings, project_keys=self._mapping_project_keys())
        except Exception:
            LOGGER.exception("workflow4-summary: story→subtask mapping failed (non-fatal)")

    def _enroll_tickets(self) -> None:
        """Enroll every phase-cached ticket that has a due date into
        ``due_date_tracking`` so the scan can alert on it.

        Driven by the per-run phase cache (current, in-scope, non-Done tickets),
        so there are no per-ticket Jira calls and no stale/deleted keys. A
        minimal ``tickets`` row is upserted first for the
        ``due_date_tracking.ticket_id`` FK; ``assignee_slack_id`` is left empty,
        so alerts route via the Governor/TL digests, not per-assignee DMs."""
        cache = getattr(self, "_phase_cache", None) or {}
        if not cache:
            return
        import psycopg2

        enrolled = 0
        with psycopg2.connect(self.settings.database_url) as conn:
            with conn.cursor() as cursor:
                for key, info in cache.items():
                    if info.get("done"):
                        continue
                    due = (
                        info.get("system_due")
                        or info.get("dev_due")
                        or info.get("qa_due")
                        or info.get("live_due")
                    )
                    if due is None:
                        continue  # nothing to track without a due date
                    try:
                        tracking_start = info.get("created") or date.today()
                        total = self._count_working_days(tracking_start, due)
                        if total <= 0:
                            total = 1
                        priority = _normalize_priority(info.get("priority"))

                        cursor.execute(
                            """
                            INSERT INTO tickets (jira_ticket_id, status)
                            VALUES (%s, 'open')
                            ON CONFLICT (jira_ticket_id)
                            DO UPDATE SET jira_ticket_id = EXCLUDED.jira_ticket_id
                            RETURNING id
                            """,
                            (key,),
                        )
                        ticket_row_id = cursor.fetchone()[0]

                        cursor.execute(
                            """
                            INSERT INTO due_date_tracking (
                                ticket_id, jira_ticket_id, priority, assignee_slack_id,
                                due_date, tracking_start_date, total_working_days
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (jira_ticket_id) DO UPDATE SET
                                due_date            = EXCLUDED.due_date,
                                tracking_start_date = EXCLUDED.tracking_start_date,
                                total_working_days  = EXCLUDED.total_working_days,
                                priority            = EXCLUDED.priority,
                                is_completed        = FALSE
                            """,
                            (ticket_row_id, key, priority, "", due, tracking_start, total),
                        )
                        conn.commit()
                        enrolled += 1
                    except Exception:
                        conn.rollback()
                        LOGGER.exception("%s: enroll failed for %s (skipping)", self.name, key)
        LOGGER.info(
            "%s: enrolled/updated %d ticket(s) in due_date_tracking", self.name, enrolled
        )

    # ── deterministic tabular digest (Jira Owner + TL) ──────────────────────
    def _build_digests(
        self, qualifying: list[dict[str, Any]], role_channels: dict[str, str | None]
    ) -> list[dict[str, Any]]:
        """Build ONE banded report (hyperlinked mrkdwn `message` + Block Kit
        `blocks`) and send it to the Jira Owner (jira_owner) and TL (eng_lead)
        channels. `qualifying` is ignored — the report is built from the phase
        cache + story_subtasks mapping."""
        try:
            message, blocks = self._build_payload()
        except Exception:
            LOGGER.exception("workflow4-summary: tabular digest build failed")
            return []
        if not message:
            return []
        channels: list[str] = []
        for role in ("jira_owner", "eng_lead"):
            channel = role_channels.get(role)
            if channel and channel not in channels:
                channels.append(channel)
        return [
            {"channel_id": channel, "message": message, "blocks": blocks}
            for channel in channels
        ]

    @staticmethod
    def _effective_due(entry: dict[str, Any]) -> Any:
        """The due date that governs a ticket: system `duedate`, else the first
        non-null phase date."""
        return (
            entry.get("system_due")
            or entry.get("dev_due")
            or entry.get("qa_due")
            or entry.get("live_due")
        )

    def _time_left(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """Working-day time-left for a cached ticket, from its own due date.
        Returns None when the ticket has no due date (undated)."""
        due = self._effective_due(entry)
        if due is None:
            return None
        today = date.today()
        start = entry.get("created") or today
        total = self._count_working_days(start, due)
        remaining = 0 if today > due else self._count_working_days(today, due)
        pct = 0.0 if total <= 0 else max(0.0, min(100.0, remaining / total * 100))
        return {"due": due, "remaining": remaining, "pct": pct}

    @staticmethod
    def _band(remaining: int, pct: float) -> str | None:
        """Bucket a ticket into a time-left band, or None if not at risk (>75%)."""
        if remaining <= 0:
            return "overdue"
        if pct <= 25:
            return "25"
        if pct <= 50:
            return "50"
        if pct <= 75:
            return "75"
        return None

    @staticmethod
    def _subtask_due(row: dict[str, Any]) -> Any:
        return (
            row.get("system_due_date")
            or row.get("dev_due_date")
            or row.get("qa_due_date")
        )

    # ── hyperlink helpers ────────────────────────────────────────────────────
    def _base_url(self) -> str:
        return (getattr(self.settings, "jira_base_url", "") or "").rstrip("/")

    def _link(self, key: Any) -> str:
        """Slack mrkdwn link to a Jira issue (plain key when no base URL)."""
        key = str(key or "—")
        base = self._base_url()
        return f"<{base}/browse/{key}|{key}>" if base and key != "—" else key

    _DIVIDER = "──────────────────────────"
    # Statuses treated as completed and hidden from the report (in addition to
    # the statusCategory=Done filter the phase-cache JQL already applies).
    _COMPLETED_STATUSES = {"completed", "not a bug"}
    _CAT_LABEL = {"overdue": "Overdue", "75": "≤75% left", "50": "≤50% left", "25": "≤25% left"}

    def _is_completed(self, status: Any) -> bool:
        return str(status or "").strip().lower() in self._COMPLETED_STATUSES

    def _cat_label(self, band: str | None) -> str:
        return self._CAT_LABEL.get(band, ">75% left")

    def _render_stories(self, stories: list[dict[str, Any]]) -> list[str]:
        """Render each Story as a header (with its own due date) followed by ALL
        its active tasks — dated tasks tagged with their time-left category,
        undated tasks marked 'no due date' — with a horizontal rule between
        Stories."""
        lines: list[str] = []
        for i, s in enumerate(stories[:_MAX_TABLE_ROWS]):
            if i:
                lines.append(self._DIVIDER)
            lines.append(
                f"*{self._link(s['key'])}* · {s['state']} · due {s['due']} · {s['assignee']}"
            )
            for t in sorted(s["tasks"], key=lambda t: t.get("_sort", (0, 0.0))):
                if t["due"] is None:
                    lines.append(
                        f"   ↳ {self._link(t['key'])} · {t['state']} · "
                        f"no due date · {t['assignee']}"
                    )
                else:
                    lines.append(
                        f"   ↳ {self._link(t['key'])} · {t['state']} · due {t['due']} "
                        f"· {t['cat']} · {t['assignee']}"
                    )
            if not s["tasks"]:
                lines.append("   _(no subtasks)_")
        if len(stories) > _MAX_TABLE_ROWS:
            lines.append(f"_…and {len(stories) - _MAX_TABLE_ROWS} more stories_")
        return lines

    # ── collect + render ─────────────────────────────────────────────────────
    def _collect(
        self, allow: Any = None, owner: Any = None
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
        """Bucket at-risk, dated, non-completed work into two banded groups,
        each keyed by time-left band (overdue / 75 / 50 / 25):
          * `bands`        — Stories ASSIGNED TO THE RECIPIENT, each carrying its
                             in-scope dated tasks (banded by the Story's time-left);
          * `other_bands`  — every other in-scope task: one with no parent Story,
                             or whose parent Story is assigned to someone else, or
                             is unassigned — banded by the task's own time-left.

        Two predicates scope the report:
          * `allow(entry)` — in scope at all (a TL's team, or one assignee).
            ``None`` = no filter (the all-teams governor report).
          * `owner(entry)` — assigned to the recipient; decides which Stories go
            to section 1. ``None`` (governor) treats any assigned Story as owned."""
        from app.story_subtasks import get_all_subtasks

        cache = getattr(self, "_phase_cache", None) or {}
        # Memoize the Story→subtask map for this run: _collect is called once per
        # recipient (each TL / assignee) and the mapping is identical every time.
        grouped = getattr(self, "_grouped_cache", None)
        if grouped is None:
            grouped = get_all_subtasks(self.settings, self._mapping_project_keys())
            self._grouped_cache = grouped

        def _ok(entry: dict[str, Any]) -> bool:
            return allow is None or allow(entry)

        def _unassigned(entry: dict[str, Any]) -> bool:
            return not str(entry.get("assignee_name") or "").strip() and \
                   not str(entry.get("assignee_email") or "").strip()

        def _owned(entry: dict[str, Any]) -> bool:
            # Section-1 Stories are those assigned to the recipient; with no
            # recipient (governor) every assigned Story qualifies.
            return (not _unassigned(entry)) if owner is None else bool(owner(entry))

        def _empty_bands() -> dict[str, list[dict[str, Any]]]:
            return {"overdue": [], "75": [], "50": [], "25": []}

        bands = _empty_bands()
        other_bands = _empty_bands()
        subtask_keys: set[str] = set()

        def _add_other(entry: dict[str, Any], key: str, due: Any, prog: dict[str, Any]) -> None:
            band = self._band(prog["remaining"], prog["pct"])
            if band is None:
                return  # not at risk
            other_bands[band].append({
                "key": key,
                "state": entry.get("status") or "—",
                "due": due,
                "assignee": entry.get("assignee_name") or "Unassigned",
                "_sort": (prog["remaining"], prog["pct"]),
            })

        for story_key, rows in grouped.items():
            for r in rows:
                subtask_keys.add(r["subtask_key"])
            entry = cache.get(story_key)
            # Skip Done (not in cache) / Completed / Not-a-Bug stories.
            if entry is None or self._is_completed(entry.get("status")):
                continue

            owned = _owned(entry)

            tasks: list[dict[str, Any]] = []
            for r in rows:
                tk = r["subtask_key"]
                tentry = cache.get(tk)
                # Only active tasks (in cache = not Done) that aren't Completed.
                if tentry is None or self._is_completed(tentry.get("status")):
                    continue
                tdue = self._effective_due(tentry)
                ttl = self._time_left(tentry)

                # A task under a Story NOT owned by the recipient (someone else's
                # or unassigned) has no section-1 home → it becomes an "other
                # task" (in-scope, dated, at-risk only), banded by its own
                # time-left.
                if not owned:
                    if _ok(tentry) and tdue is not None and ttl is not None:
                        _add_other(tentry, tk, tdue, ttl)
                    continue

                # Under a Story the recipient owns, list EVERY active subtask —
                # any assignee, with or without a due date — so the owner sees
                # the full breakdown. Undated tasks sort last (carry no time-left).
                cat = self._cat_label(self._band(ttl["remaining"], ttl["pct"])) if ttl else "—"
                tasks.append({
                    "key": tk,
                    "state": tentry.get("status") or "—",
                    "due": tdue,  # None → rendered as "no due date"
                    "cat": cat,
                    "assignee": tentry.get("assignee_name") or "Unassigned",
                    "_sort": (ttl["remaining"], ttl["pct"]) if ttl else (10**9, 10**9),
                })

            # Only Stories assigned to the recipient are listed in section 1.
            if not owned:
                continue

            # The owned Story needs its own due date + at-risk band to be listed.
            due = self._effective_due(entry)
            if due is None:
                continue  # skip Stories with no due date
            tl = self._time_left(entry)
            band = self._band(tl["remaining"], tl["pct"]) if tl else None
            if band is None:
                continue  # Story not at risk (>75% time left)
            bands[band].append({
                "key": story_key,
                "state": entry.get("status") or "—",
                "due": due,
                "assignee": entry.get("assignee_name") or "Unassigned",
                "tasks": tasks,
            })

        story_keys = set(grouped.keys())
        for key, entry in cache.items():
            if key in story_keys or key in subtask_keys:
                continue
            if str(entry.get("issuetype") or "").strip().lower() == "story":
                continue
            if self._is_completed(entry.get("status")):
                continue
            due = self._effective_due(entry)
            if due is None:
                continue  # skip tasks with no due date
            if not _ok(entry):
                continue  # out of scope
            tl = self._time_left(entry)
            if tl is None:
                continue
            _add_other(entry, key, due, tl)
        return bands, other_bands

    def _render_other_tasks(self, tasks: list[dict[str, Any]]) -> list[str]:
        """Top-level task lines (no parent Story) for the Other Tasks section,
        overdue-first. The band heading already states the time left, so the
        per-row category is omitted."""
        ordered = sorted(tasks, key=lambda t: t.get("_sort", (0, 0.0)))
        lines = [
            f"{self._link(t['key'])} · {t['state']} · due {t['due']} · {t['assignee']}"
            for t in ordered[:_MAX_TABLE_ROWS]
        ]
        if len(ordered) > _MAX_TABLE_ROWS:
            lines.append(f"_…and {len(ordered) - _MAX_TABLE_ROWS} more_")
        return lines

    def _banded_section(
        self,
        header: str,
        band_dict: dict[str, list[dict[str, Any]]],
        render: Any,
    ) -> list[tuple[str, list[str]]]:
        """A numbered section split into a/b/c/d time-left bands; empty bands and
        the whole section are omitted. `render(items)` turns a band's items into
        message lines."""
        if not any(band_dict.values()):
            return []
        out: list[tuple[str, list[str]]] = [(header, [])]
        for band_key, label in _BAND_ORDER:
            items = band_dict[band_key]
            if not items:
                continue
            out.append((f"*{label}* ({len(items)})", render(items)))
        return out

    def _groups(
        self, allow: Any = None, title_prefix: str | None = None, owner: Any = None
    ) -> list[tuple[str, list[str]]] | None:
        """Heading + line groups shared by the mrkdwn and Block Kit renderers,
        or None when nothing is at risk. Empty bands/sections are omitted.
        `allow`/`owner`/`title_prefix` scope the report to one recipient (see
        `_collect`)."""
        bands, other_bands = self._collect(allow, owner)
        if not any(bands.values()) and not any(other_bands.values()):
            return None

        scope_label = ", ".join(self._mapping_project_keys() or []) or self._scope_label()
        label_prefix = title_prefix or "Due-Date Compliance"
        groups: list[tuple[str, list[str]]] = [
            (f"*{label_prefix} — {scope_label} — {date.today():%Y-%m-%d}*", []),
        ]
        groups += self._banded_section("*1. Stories*", bands, self._render_stories)
        groups += self._banded_section(
            "*2. Other Tasks*", other_bands, self._render_other_tasks
        )
        return groups

    @staticmethod
    def _chunk(text: str, limit: int) -> list[str]:
        if len(text) <= limit:
            return [text]
        out: list[str] = []
        cur = ""
        for ln in text.split("\n"):
            if cur and len(cur) + len(ln) + 1 > limit:
                out.append(cur)
                cur = ln
            else:
                cur = f"{cur}\n{ln}" if cur else ln
        if cur:
            out.append(cur)
        return out

    def _render_payload(
        self, groups: list[tuple[str, list[str]]] | None
    ) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Turn heading+line `groups` into (mrkdwn text, Block Kit blocks).
        Returns (None, None) when there is nothing to send."""
        if groups is None:
            return None, None

        text = "\n\n".join(
            heading + ("\n" + "\n".join(lines) if lines else "")
            for heading, lines in groups
        )

        blocks: list[dict[str, Any]] = [
            {"type": "header",
             "text": {"type": "plain_text", "text": groups[0][0].strip("*")[:150]}}
        ]
        for heading, lines in groups[1:]:
            body = heading + ("\n" + "\n".join(lines) if lines else "")
            for chunk in self._chunk(body, 2900):
                if len(blocks) >= 48:
                    break
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
        return text, blocks

    def _build_payload(
        self, allow: Any = None, title_prefix: str | None = None, owner: Any = None
    ) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Hierarchical Story/Task report (governor + per-TL). text is None when
        nothing is at risk. `allow`/`owner`/`title_prefix` scope the report to
        one recipient (see `_collect`)."""
        return self._render_payload(self._groups(allow, title_prefix, owner))

    # Per-assignee digest bands (flat, no Story/Task hierarchy).
    _ASSIGNEE_BANDS = [
        ("overdue", "1. Overdue tickets"),
        ("75", "2. Tickets with <75% Time"),
        ("50", "3. Tickets with <50% Time"),
        ("25", "4. Tickets with <25% Time"),
    ]

    def _build_assignee_payload(
        self, allow: Any, title_prefix: str
    ) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Flat per-assignee digest: every at-risk, dated ticket assigned to the
        person (ANY issue type — Stories and tasks alike, ignoring the Story
        hierarchy), grouped into four time-left bands. A personal to-do list by
        urgency, distinct from the Story/Task report TLs/governor get."""
        cache = getattr(self, "_phase_cache", None) or {}
        bands: dict[str, list[dict[str, Any]]] = {"overdue": [], "75": [], "50": [], "25": []}
        for key, entry in cache.items():
            if self._is_completed(entry.get("status")):
                continue
            if not allow(entry):
                continue
            due = self._effective_due(entry)
            if due is None:
                continue  # undated → no urgency band
            tl = self._time_left(entry)
            if tl is None:
                continue
            band = self._band(tl["remaining"], tl["pct"])
            if band is None:
                continue  # >75% left, not at risk
            bands[band].append({
                "key": key,
                "state": entry.get("status") or "—",
                "due": due,
                "_sort": (tl["remaining"], tl["pct"]),
            })
        if not any(bands.values()):
            return None, None

        scope_label = ", ".join(self._mapping_project_keys() or []) or self._scope_label()
        groups: list[tuple[str, list[str]]] = [
            (f"*{title_prefix} — {scope_label} — {date.today():%Y-%m-%d}*", []),
        ]
        for band_key, label in self._ASSIGNEE_BANDS:
            items = bands[band_key]
            if not items:
                continue  # omit empty bands
            ordered = sorted(items, key=lambda t: t["_sort"])
            lines = [
                f"{self._link(t['key'])} · {t['state']} · due {t['due']}"
                for t in ordered[:_MAX_TABLE_ROWS]
            ]
            if len(ordered) > _MAX_TABLE_ROWS:
                lines.append(f"_…and {len(ordered) - _MAX_TABLE_ROWS} more_")
            groups.append((f"*{label}* ({len(items)})", lines))
        return self._render_payload(groups)

    @staticmethod
    def _entry_in_team(entry: dict[str, Any], members: set[str]) -> bool:
        """True when a phase-cache entry's assignee (by email, else display
        name) is one of the given identities. Used by both the per-TL and the
        per-assignee `allow` filters (a single person is a one-member set)."""
        email = str(entry.get("assignee_email") or "").strip().lower()
        if email and email in members:
            return True
        name = str(entry.get("assignee_name") or "").strip().lower().lstrip("@")
        return bool(name and name in members)


# ── per-assignee morning digest (09:00) ──────────────────────────────────────
class _AssigneeReportMixin:
    """09:00 behaviour: DM each assignee a flat 4-band to-do digest of *their
    own* at-risk tickets (Overdue / <75% / <50% / <25% time left), and still
    send the full hierarchical report to the Jira Owner. Mixed before
    `_Workflow4SummaryMixin`, so its `_build_digests` wins."""

    def _build_digests(
        self, qualifying: list[dict[str, Any]], role_channels: dict[str, str | None]
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        try:
            alerts.extend(self._assignee_reports())
        except Exception:
            LOGGER.exception("%s: per-assignee digests failed", self.name)
        # Governor copy — the consolidated report still goes to jira_owner.
        try:
            governor = self._clean_channel(role_channels.get("jira_owner"))
            if governor:
                message, blocks = self._build_payload()
                if message:
                    alerts.append(
                        {"channel_id": governor, "message": message, "blocks": blocks}
                    )
        except Exception:
            LOGGER.exception("%s: governor copy failed", self.name)
        return alerts

    def _channel_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """One read of channelid_table → (email→channel, slack_name→channel)."""
        import psycopg2
        from psycopg2.extras import RealDictCursor

        by_email: dict[str, str] = {}
        by_name: dict[str, str] = {}
        try:
            with psycopg2.connect(self.settings.database_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT email_id, slack_user_name, channel_id FROM channelid_table"
                    )
                    for row in cur.fetchall():
                        channel = str(row.get("channel_id") or "").strip()
                        if not channel:
                            continue
                        email = str(row.get("email_id") or "").strip().lower()
                        name = str(row.get("slack_user_name") or "").strip().lower().lstrip("@")
                        if email:
                            by_email[email] = channel
                        if name:
                            by_name[name] = channel
        except Exception:
            LOGGER.exception("%s: channelid_table lookup failed", self.name)
        return by_email, by_name

    def _team_lead_identities(self) -> set[str]:
        """Lower-cased emails + slack names of users flagged ``is_team_lead``.
        The 09:00 per-assignee digest skips them: a TL's own at-risk tickets are
        already covered by their 15:00 team digest, so we don't DM them twice."""
        import psycopg2
        from psycopg2.extras import RealDictCursor

        ids: set[str] = set()
        try:
            with psycopg2.connect(self.settings.database_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "ALTER TABLE channelid_table "
                        "ADD COLUMN IF NOT EXISTS is_team_lead BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                    conn.commit()
                    cur.execute(
                        "SELECT email_id, slack_user_name FROM channelid_table "
                        "WHERE is_team_lead = TRUE"
                    )
                    for row in cur.fetchall():
                        email = str(row.get("email_id") or "").strip().lower()
                        name = str(row.get("slack_user_name") or "").strip().lower().lstrip("@")
                        if email:
                            ids.add(email)
                        if name:
                            ids.add(name)
        except Exception:
            LOGGER.exception("%s: team-lead lookup failed", self.name)
        return ids

    def _assignee_reports(self) -> list[dict[str, Any]]:
        """One flat, 4-band to-do digest per assignee who has at-risk work and a
        DM channel — every at-risk ticket assigned to them, by urgency. Team
        leads are skipped here; they receive their work in the 15:00 team digest."""
        cache = getattr(self, "_phase_cache", None) or {}
        by_email, by_name = self._channel_maps()
        tl_ids = self._team_lead_identities()

        # Gather distinct assignees with at-risk, dated, active work and resolve
        # each to a DM channel. `people` is keyed by channel so a person mapped
        # by both email and name isn't DM'd twice.
        people: dict[str, dict[str, Any]] = {}
        for entry in cache.values():
            if self._is_completed(entry.get("status")):
                continue
            if self._effective_due(entry) is None:
                continue
            tl = self._time_left(entry)
            if tl is None or self._band(tl["remaining"], tl["pct"]) is None:
                continue
            name = str(entry.get("assignee_name") or "").strip()
            email = str(entry.get("assignee_email") or "").strip()
            if not name and not email:
                continue  # unassigned → only in the governor report
            # Skip team leads at 09:00 — covered by their 15:00 team digest.
            if (email and email.lower() in tl_ids) or (
                name and name.lower().lstrip("@") in tl_ids
            ):
                LOGGER.info(
                    "%s: skipping 09:00 digest for team lead %s <%s> — covered at 15:00",
                    self.name, name, email,
                )
                continue
            channel = by_email.get(email.lower()) if email else None
            if not channel and name:
                channel = by_name.get(name.lower().lstrip("@"))
            if not channel:
                LOGGER.info(
                    "%s: no channelid_table match for assignee %s <%s> — skipping DM",
                    self.name, name, email,
                )
                continue
            person = people.setdefault(channel, {"name": name or email, "ids": set()})
            if email:
                person["ids"].add(email.lower())
            if name:
                person["ids"].add(name.lower().lstrip("@"))

        alerts: list[dict[str, Any]] = []
        for channel, person in people.items():
            allow = lambda entry, m=person["ids"]: self._entry_in_team(entry, m)
            message, blocks = self._build_assignee_payload(
                allow, title_prefix=f"Due-date digest for {person['name']}"
            )
            if message:
                alerts.append(
                    {"channel_id": channel, "message": message, "blocks": blocks}
                )
        return alerts


# ── per-team-lead digest (15:00) ─────────────────────────────────────────────
class _TLReportMixin:
    """15:00 behaviour: instead of one consolidated digest to a single channel,
    send each Team Lead a digest of *only their own team's* at-risk tickets
    (tickets assigned to the TL or to a member of their team). The full
    consolidated report still goes to the Jira Owner for oversight.

    Team membership lives in channelid_table: a TL's row has
    ``is_team_lead = TRUE``; each member's row carries ``team_lead_email``.
    Mixed before ``_Workflow4SummaryMixin`` so this ``_build_digests`` wins."""

    def _build_digests(
        self, qualifying: list[dict[str, Any]], role_channels: dict[str, str | None]
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        # Per-TL: the SAME hierarchical Story/Task report as the governor's, but
        # scoped to each TL's own team (tickets assigned to the TL or a member).
        tl_alerts: list[dict[str, Any]] = []
        try:
            tl_alerts = self._tl_reports()
            alerts.extend(tl_alerts)
        except Exception:
            LOGGER.exception("%s: per-TL digests failed", self.name)
        governor = self._clean_channel(role_channels.get("jira_owner"))
        # Governor copy — the full, all-teams report still goes to the Jira Owner.
        try:
            if governor:
                message, blocks = self._build_payload()
                if message:
                    alerts.append(
                        {"channel_id": governor, "message": message, "blocks": blocks}
                    )
        except Exception:
            LOGGER.exception("%s: governor copy failed", self.name)
        # Per-TL copy to the Jira Owner — forward each TL's own digest to the
        # owner as its own message, so the owner receives one message per mapped
        # TL (each already titled "Team digest for <TL>"). This is in addition to
        # the consolidated report above.
        if governor:
            for tl_alert in tl_alerts:
                alerts.append(
                    {
                        "channel_id": governor,
                        "message": tl_alert["message"],
                        "blocks": tl_alert["blocks"],
                    }
                )
        return alerts

    def _team_maps(self) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """One read of channelid_table → (assignee identity → TL email,
        TL email → {channel, name}). Identity is the lower-cased email_id or
        slack_user_name; a TL is also its own identity so its tickets route to
        itself. Self-heals the two team columns so the digest works before the
        seed SQL is applied."""
        import psycopg2
        from psycopg2.extras import RealDictCursor

        identity_to_tl: dict[str, str] = {}
        tl_info: dict[str, dict[str, str]] = {}
        try:
            with psycopg2.connect(self.settings.database_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "ALTER TABLE channelid_table "
                        "ADD COLUMN IF NOT EXISTS team_lead_email TEXT"
                    )
                    cur.execute(
                        "ALTER TABLE channelid_table "
                        "ADD COLUMN IF NOT EXISTS is_team_lead BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                    conn.commit()
                    cur.execute(
                        "SELECT email_id, slack_user_name, channel_id, "
                        "team_lead_email, is_team_lead FROM channelid_table"
                    )
                    rows = cur.fetchall()
        except Exception:
            LOGGER.exception("%s: channelid_table team lookup failed", self.name)
            return identity_to_tl, tl_info

        # Pass 1 — TLs with a usable channel become digest targets.
        for row in rows:
            if not row.get("is_team_lead"):
                continue
            email = str(row.get("email_id") or "").strip().lower()
            channel = self._clean_channel(row.get("channel_id"))
            if not email:
                continue
            if not channel:
                LOGGER.info(
                    "%s: TL %s has no channel_id — their team's tickets stay unrouted",
                    self.name, email,
                )
                continue
            tl_info[email] = {
                "channel": channel,
                "name": str(row.get("slack_user_name") or email),
            }
            identity_to_tl[email] = email  # a TL owns their own tickets
            name = str(row.get("slack_user_name") or "").strip().lower().lstrip("@")
            if name:
                identity_to_tl[name] = email

        # Pass 2 — members point at their TL (only if that TL is a live target).
        for row in rows:
            tl_email = str(row.get("team_lead_email") or "").strip().lower()
            if not tl_email or tl_email not in tl_info:
                continue
            email = str(row.get("email_id") or "").strip().lower()
            name = str(row.get("slack_user_name") or "").strip().lower().lstrip("@")
            if email:
                identity_to_tl[email] = tl_email
            if name:
                identity_to_tl[name] = tl_email
        return identity_to_tl, tl_info

    def _tl_reports(self) -> list[dict[str, Any]]:
        """One hierarchical Story/Task report per Team Lead, filtered to that
        team's tickets — identical format to the governor report."""
        identity_to_tl, tl_info = self._team_maps()
        if not tl_info:
            LOGGER.info(
                "%s: no team leads configured (is_team_lead) — skipping per-TL digests",
                self.name,
            )
            return []

        # Invert the identity→TL map into TL→{member identities}.
        members_by_tl: dict[str, set[str]] = {}
        for identity, tl_email in identity_to_tl.items():
            members_by_tl.setdefault(tl_email, set()).add(identity)

        alerts: list[dict[str, Any]] = []
        for tl_email, info in tl_info.items():
            members = members_by_tl.get(tl_email, set())
            # Section 1 = Stories assigned to the TL themselves; section 2 = the
            # rest of the team's tasks. The TL's own identities are their email
            # plus their slack name.
            own_ids = {tl_email}
            tl_name = str(info.get("name") or "").strip().lower().lstrip("@")
            if tl_name:
                own_ids.add(tl_name)
            allow = lambda entry, m=members: self._entry_in_team(entry, m)
            owner = lambda entry, o=own_ids: self._entry_in_team(entry, o)
            message, blocks = self._build_payload(
                allow=allow, owner=owner, title_prefix=f"Team digest for {info['name']}"
            )
            if message:
                alerts.append(
                    {"channel_id": info["channel"], "message": message, "blocks": blocks}
                )
        return alerts


# ── production (all spaces) ──────────────────────────────────────────────────
class Workflow4SummaryAssigneeChecker(
    _AssigneeReportMixin, _Workflow4SummaryMixin, Workflow4AssigneeChecker
):
    """Production 09:00 batch — under 75% time left, enriched + summarized."""

    name = "workflow4-summary-daily-assignee"


class Workflow4SummaryTLChecker(_TLReportMixin, _Workflow4SummaryMixin, Workflow4TLChecker):
    """Production 15:00 batch — enriched + summarized. Each Team Lead gets a
    digest of only their own team's at-risk tickets; the Jira Owner still gets
    the full consolidated report."""

    name = "workflow4-summary-tl"


# ── AIGOV sandbox (test scope + auto-enroll) ─────────────────────────────────
class _AigovFlowMixin(_Workflow4SummaryMixin):
    """Restrict the enriched flow to the AIGOV sandbox. Auto-enrollment is
    inherited from the shared mixin (Workflow4 is self-sufficient)."""

    project_key = AIGOV_PROJECT_KEY
    summary_force_refresh = True

    def _build_embeddings_enabled(self) -> bool:
        # AIGOV is a small sandbox — keep embeddings on for parity with the
        # original test flow regardless of the production toggle.
        return True

    def _scope_jql(self) -> str:
        return f'project = "{AIGOV_PROJECT_KEY}"'

    def _fetch_tickets(self) -> list[dict[str, Any]]:
        from app.jira_fetcher import fetch_project_tickets

        return fetch_project_tickets(AIGOV_PROJECT_KEY, force_refresh=True)

    def _mapping_project_keys(self) -> list[str] | None:
        return [AIGOV_PROJECT_KEY]


class Workflow4AigovAssigneeChecker(
    _AssigneeReportMixin, _AigovFlowMixin, Workflow4AssigneeChecker
):
    """AIGOV 09:00 batch — under 75% time left."""

    name = "workflow4-aigov-daily-assignee"


class Workflow4AigovTLChecker(_AigovFlowMixin, Workflow4TLChecker):
    """AIGOV 15:00 batch — 50% or less time left."""

    name = "workflow4-aigov-tl"
