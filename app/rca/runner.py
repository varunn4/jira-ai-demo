"""Phase H — RCA pipeline orchestration (read-only end to end).

Ties the phases together for one Jira key:
  C intake → similar tickets → D localize → ensure fix links →
  E retrieve → F agent investigation → G synthesize → ownership (git blame) →
  delivery decision (deliver vs low_confidence) → optional Jira comment (dry-run).

Nothing here writes to a repo, runs code, or applies a change.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import Settings
from app.rca import (
    agent, fix_links, intake as intake_mod, localize, repos as repo_access,
    retrieval, synthesis,
)
from app.rca.store import (
    RCARun, RCARunStore, STATUS_DELIVERED, STATUS_FAILED, STATUS_INVESTIGATING,
    STATUS_LOW_CONFIDENCE, STATUS_SYNTHESIZING,
)

log = logging.getLogger(__name__)


def _similar_ticket_keys(settings: Settings, summary: str, description: str,
                         project_key: str, limit: int = 5) -> list[str]:
    try:
        from app.similar_ticket_finder import SimilarTicketFinder
        finder = SimilarTicketFinder(settings)
        res = finder.find_similar(summary, description, project_key=project_key)
        return [t.get("ticket_key") for t in res.get("tickets", []) if t.get("ticket_key")][:limit]
    except Exception as exc:
        log.warning("similar-ticket lookup failed: %s", exc)
        return []


def _append_blame_evidence(settings: Settings, diagnosis: dict[str, Any]) -> None:
    """Add git history for the root-cause lines as a NEUTRAL evidence fact.

    RULE 9: git blame shows who last changed a line, not who is responsible — so
    this records the commit that last touched the suspected lines as supporting
    evidence, without attributing blame to a person. Skipped when the run is
    undetermined/insufficient or the lines aren't localized.
    """
    root_cause = str(diagnosis.get("root_cause") or "")
    if root_cause in ("Undetermined.",) or root_cause.startswith("INSUFFICIENT DATA"):
        return
    loc = diagnosis.get("root_cause_location") or {}
    repo, path, lines = loc.get("repo"), loc.get("file"), loc.get("lines")
    if not (repo and path and lines):
        return
    try:
        start, _, end = str(lines).partition("-")
        s = int(start.strip())
        e = int(end.strip()) if end.strip() else s
        blame = repo_access.git_blame(settings, repo, path, s, e)
    except Exception:
        return
    if not blame:
        return
    last = blame[-1]
    sha = str(last.get("sha") or "")[:10]
    if not sha:
        return
    diagnosis.setdefault("evidence", []).append({
        "category": "Git",
        "detail": f"Suspected lines were last modified in commit {sha}",
        "ref": f"{path}:{s}-{e}",
    })
    # NOTE: this is the LAST-MODIFYING commit, recorded as neutral Git evidence
    # only. It deliberately does NOT populate `introduced_by` — RULES 10/11
    # forbid using the last-modifying commit as the introducing commit. Authorship
    # is set by synthesis only when git history verifies the introducing commit.


def run_pipeline(
    settings: Settings,
    store: RCARunStore,
    run: RCARun,
    *,
    intake_client: Any = None,
    synthesis_client: Any = None,
) -> RCARun:
    """Execute the full RCA pipeline for an already-created run. Read-only."""
    try:
        store.touch(run, status=STATUS_INVESTIGATING)

        # ── C: intake & extraction ──────────────────────────────────────────
        ticket_obj = intake_mod.fetch_ticket(settings, run.jira_key)
        extracted = intake_mod.extract_signals(settings, ticket_obj, llm_client=intake_client)
        ticket = ticket_obj.to_dict()
        project_key = run.jira_key.rsplit("-", 1)[0] if "-" in run.jira_key else run.jira_key
        query_text = f"{ticket_obj.summary}\n{ticket_obj.description}".strip()

        # ── similar tickets (signal for D & E) ──────────────────────────────
        similar_keys = _similar_ticket_keys(
            settings, ticket_obj.summary, ticket_obj.description, project_key)
        # ensure their fix links exist for the retrieval signal
        for key in similar_keys:
            if not fix_links.get_links(settings, key):
                fix_links.link_ticket(settings, key)

        # ── D: localize ─────────────────────────────────────────────────────
        candidates_repos = localize.localize(
            settings, ticket_key=run.jira_key, components=ticket_obj.components,
            project_key=project_key, query_text=query_text,
            similar_ticket_keys=similar_keys,
        )
        run.localized_repos = [
            {"repo": c.repo, "score": round(c.score, 3), "reasons": c.reasons}
            for c in candidates_repos
        ]
        all_repos = repo_access.list_repos(settings)
        repo_names = [c.repo for c in candidates_repos] or all_repos[:3]
        store.touch(run)

        # ── E: hybrid retrieval ─────────────────────────────────────────────
        # Retrieval (and thus the seeded candidates) stays scoped to the localized
        # repos so the agent starts focused where the defect most likely lives.
        candidates = retrieval.retrieve(
            settings, repos=repo_names,
            error_messages=extracted.get("error_messages", []),
            stack_frames=extracted.get("stack_frames", []),
            query_text=query_text, similar_ticket_keys=similar_keys,
        )
        run.candidates = [c.to_dict() for c in candidates]
        store.touch(run)

        # ── F: agentic investigation (read-only) ────────────────────────────
        # The agent may read ANY available repo, not just the localized ones: the
        # feature under investigation often spans a repo the localizer missed (a
        # legacy backend, or the frontend where a UI label like "AML Leads" lives).
        # Every tool is read-only, so a broad scope adds no risk — only the seed
        # focuses the agent, the scope must not wall it off. Falls back to the
        # localized repos if the repo root can't be listed.
        agent_res = agent.run_investigation(
            settings, ticket_summary=ticket_obj.summary, extracted=extracted,
            candidates=run.candidates, allowed_repos=(all_repos or repo_names),
            ticket_description=ticket_obj.description,
            ticket_comments=ticket_obj.comments,
            on_event=lambda ev: store.add_trace_event(run, ev),
        )
        run.agent_trace = agent_res.agent_trace
        store.touch(run, status=STATUS_SYNTHESIZING)

        # ── G: synthesis ────────────────────────────────────────────────────
        diagnosis = synthesis.synthesize(
            settings, ticket=ticket, extracted=extracted, candidates=run.candidates,
            investigation=agent_res.summary, trace=run.agent_trace,
            llm_client=synthesis_client,
        )
        _append_blame_evidence(settings, diagnosis)
        run.diagnosis = diagnosis
        run.confidence = diagnosis.get("confidence")

        # ── build the house-template document ───────────────────────────────
        from app.rca import rca_document
        run.document = rca_document.build_document(ticket, extracted, diagnosis,
                                                   agent_meta={
                                                       "iterations": agent_res.iterations,
                                                       "stop_reason": agent_res.stop_reason,
                                                   })

        # ── delivery decision ───────────────────────────────────────────────
        deliver = (run.confidence or 0.0) >= settings.rca_confidence_threshold
        final_status = STATUS_DELIVERED if deliver else STATUS_LOW_CONFIDENCE
        store.touch(run, status=final_status)

        if deliver and settings.rca_jira_post_enabled:
            _post_to_jira(settings, run)

        log.info("RCA run %s finished: status=%s confidence=%s",
                 run.run_id, final_status, run.confidence)
        return run

    except Exception as exc:  # surface failure on the run, never crash the worker
        log.exception("RCA run %s failed", run.run_id)
        run.error = f"{type(exc).__name__}: {exc}"[:1000]
        store.touch(run, status=STATUS_FAILED)
        return run


def _post_to_jira(settings: Settings, run: RCARun) -> None:
    """Post the diagnosis as a Jira comment (only when explicitly enabled)."""
    try:
        from app.rca import rca_document
        body = rca_document.render_markdown(run.document)
        from app.jira_client import JiraClient  # existing client
        JiraClient(settings).add_comment(run.jira_key, body)
        log.info("RCA diagnosis posted to Jira %s", run.jira_key)
    except Exception as exc:
        log.warning("RCA Jira post failed for %s: %s", run.jira_key, exc)
