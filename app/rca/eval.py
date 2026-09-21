"""Phase I — offline evaluation of RCA file-localization.

Takes historical resolved defects whose fixing commits are known (from
`rca_ticket_fix_links`), HIDES the fix, runs localization+retrieval, and reports
top-k file-localization accuracy: did a predicted file fall within the files the
fix actually changed? The fix commits are used ONLY as ground-truth labels for
scoring — never shown to the pipeline at inference time.

A ticket's own fix link is excluded from every inference signal (similar-ticket
retriever, etc.), so the model cannot "see" its own answer. The default scorer
is LLM-free (uses retrieval candidates) so the harness is cheap to run over many
tickets; an optional sampled explanation-plausibility check is provided for the
full pipeline.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import Settings
from app.rca import fix_links, localize, retrieval

log = logging.getLogger(__name__)


@dataclass
class TicketLabel:
    key: str
    project_key: str
    summary: str
    description: str
    changed_files: set[str]  # ground truth (hidden at inference)


@dataclass
class EvalCase:
    key: str
    predicted_files: list[str]
    ground_truth: list[str]
    hit_at: dict[int, bool] = field(default_factory=dict)


# ── labels ────────────────────────────────────────────────────────────────────

def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def gather_labeled(settings: Settings, project_keys: Optional[list[str]] = None,
                   limit: int = 100) -> list[TicketLabel]:
    """Resolved tickets that have fix links + cached text. Ground truth = files."""
    if not settings.database_url:
        return []
    sql = (
        "SELECT c.ticket_key, c.project_key, c.summary, c.description, "
        "       jsonb_agg(DISTINCT f.changed_files) AS files_arrays "
        "FROM jira_ticket_cache c "
        "JOIN rca_ticket_fix_links f ON f.ticket_key = c.ticket_key "
        "WHERE jsonb_array_length(f.changed_files) > 0 "
    )
    params: list[Any] = []
    if project_keys:
        sql += "AND c.project_key = ANY(%s) "
        params.append(project_keys)
    sql += "GROUP BY c.ticket_key, c.project_key, c.summary, c.description "
    sql += f"LIMIT {int(limit)}"

    with _connect(settings) as conn:
        rows = conn.execute(sql, params or None).fetchall()

    labels: list[TicketLabel] = []
    for r in rows:
        files: set[str] = set()
        for arr in (r["files_arrays"] or []):
            for path in (arr or []):
                files.add(path)
        if not files:
            continue
        labels.append(TicketLabel(
            key=r["ticket_key"], project_key=r["project_key"] or "",
            summary=r["summary"] or "", description=r["description"] or "",
            changed_files=files,
        ))
    return labels


# ── inference (fix hidden) ────────────────────────────────────────────────────

def predict_files(settings: Settings, label: TicketLabel, top_k: int = 10) -> list[str]:
    """Localization+retrieval prediction with the ticket's own fix hidden."""
    query_text = f"{label.summary}\n{label.description}".strip()

    # similar tickets, EXCLUDING self (the ticket's own fix must stay hidden)
    similar_keys: list[str] = []
    try:
        from app.similar_ticket_finder import SimilarTicketFinder
        res = SimilarTicketFinder(settings).find_similar(
            label.summary, label.description, project_key=label.project_key)
        similar_keys = [t["ticket_key"] for t in res.get("tickets", [])
                        if t.get("ticket_key") and t["ticket_key"] != label.key]
    except Exception as exc:
        log.debug("eval similar lookup failed for %s: %s", label.key, exc)

    repo_cands = localize.localize(
        settings, ticket_key=label.key, components=[], project_key=label.project_key,
        query_text=query_text, similar_ticket_keys=similar_keys,
    )
    repo_names = [c.repo for c in repo_cands]
    if not repo_names:
        return []

    candidates = retrieval.retrieve(
        settings, repos=repo_names, error_messages=[], stack_frames=[],
        query_text=query_text, similar_ticket_keys=similar_keys, top_k=top_k,
    )
    # ordered unique predicted files
    seen: set[str] = set()
    files: list[str] = []
    for c in candidates:
        if c.file_path not in seen:
            seen.add(c.file_path)
            files.append(c.file_path)
    return files


# ── scoring ───────────────────────────────────────────────────────────────────

def _score_case(label: TicketLabel, predicted: list[str], ks: list[int]) -> EvalCase:
    gt = label.changed_files
    case = EvalCase(key=label.key, predicted_files=predicted, ground_truth=sorted(gt))
    for k in ks:
        case.hit_at[k] = any(p in gt for p in predicted[:k])
    return case


def run_eval(settings: Settings, project_keys: Optional[list[str]] = None,
             limit: int = 100, ks: Optional[list[int]] = None) -> dict[str, Any]:
    """Run the localization eval and return a top-k accuracy report."""
    ks = ks or [1, 3, 5]
    labels = gather_labeled(settings, project_keys, limit)
    if not labels:
        return {"tickets": 0, "note": "no labeled tickets with fix links found"}

    cases: list[EvalCase] = []
    for label in labels:
        try:
            predicted = predict_files(settings, label, top_k=max(ks))
        except Exception as exc:
            log.warning("eval prediction failed for %s: %s", label.key, exc)
            predicted = []
        cases.append(_score_case(label, predicted, ks))

    n = len(cases)
    topk = {f"top_{k}": round(sum(1 for c in cases if c.hit_at.get(k)) / n, 4) for k in ks}
    return {
        "tickets": n,
        "top_k_accuracy": topk,
        "cases": [
            {"key": c.key, "hit_at": c.hit_at,
             "predicted": c.predicted_files[:max(ks)], "ground_truth": c.ground_truth}
            for c in cases
        ],
    }


def explanation_plausibility_sample(
    settings: Settings, sample_keys: list[str], judge_client: Any = None,
) -> list[dict[str, Any]]:
    """Run the FULL pipeline on a few tickets and LLM-judge explanation plausibility.

    Ground-truth files are still withheld from the pipeline; they are only shown
    to the judge for scoring. Returns per-ticket plausibility verdicts.
    """
    from app.rca.runner import run_pipeline
    from app.rca.store import RCARunStore

    store = RCARunStore(settings)
    out: list[dict[str, Any]] = []
    for key in sample_keys:
        gt = {l.changed_files for l in gather_labeled(settings, None, 1000) if l.key == key}
        run = store.create(key)
        run = run_pipeline(settings, store, run)
        diag = run.diagnosis or {}
        verdict = _judge_plausibility(settings, diag, gt, judge_client)
        out.append({"key": key, "confidence": run.confidence,
                    "root_cause": diag.get("root_cause"), "plausibility": verdict})
    return out


def _judge_plausibility(settings: Settings, diagnosis: dict[str, Any],
                        ground_truth: Any, judge_client: Any) -> dict[str, Any]:
    if judge_client is None:
        try:
            from app.llm_client import build_llm_client
            judge_client = build_llm_client(settings)
        except Exception:
            return {"verdict": "unavailable"}
    prompt = (
        "Judge whether this root-cause explanation is plausible and consistent with "
        "the files the fix actually changed. Return JSON {\"plausible\": bool, "
        "\"reason\": str, \"score\": 0..1}.\n\n"
        f"DIAGNOSIS:\n{json.dumps({k: diagnosis.get(k) for k in ('issue_classification','confidence_label','root_cause','root_cause_location','evidence')}, indent=2)}\n\n"
        f"GROUND-TRUTH changed files:\n{json.dumps(list(ground_truth), default=list)}"
    )
    from app.json_utils import parse_model_json
    try:
        return parse_model_json(judge_client.complete("You are a strict RCA reviewer.", prompt, max_tokens=500))
    except Exception as exc:
        return {"verdict": "error", "error": str(exc)}
