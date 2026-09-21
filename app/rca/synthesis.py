"""Phase G — evidence-first root-cause synthesis.

Turns the intake + retrieval candidates + agent findings into a strict,
evidence-grounded diagnosis. The contract (see `_SYSTEM`) optimizes for being
CORRECT, not for producing a complete report:

* the defect is classified into exactly one category;
* facts, inferences and unknowns are kept separate;
* a root cause is asserted ONLY with ≥2 independent pieces of evidence, else it
  stays "Undetermined";
* a root cause is CONFIRMED (header "ROOT CAUSE") only when it explains every
  symptom, has ≥2 independent evidence sources, and has no contradictory evidence;
  otherwise it is reported as "MOST LIKELY ROOT CAUSE" with the evidence still
  required to confirm it;
* confidence is categorical (High/Medium/Low) — never an invented probability;
* if the required evidence is unavailable, the whole run is flagged
  INSUFFICIENT DATA and nothing is speculated;
* the NEXT ACTION matches confidence — a fix at High, verification steps at
  Medium, additional evidence required at Low;
* authorship (introducing commit) is reported ONLY from git history, never
  inferred, never framed as blame.

Any suggested fix is advisory — never applied, never written to a worktree.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.config import Settings
from app.json_utils import parse_model_json

log = logging.getLogger(__name__)

# The one-of classification set (RULE 3). Anything else normalizes to
# "Cannot Determine".
ISSUE_CLASSES = (
    "Runtime Bug", "Missing Implementation", "Configuration Issue",
    "Infrastructure Issue", "Deployment Issue", "Data Issue", "Requirement Gap",
    "Third-party Dependency", "Performance Issue", "Security Issue",
    "Cannot Determine",
)

CONFIDENCE_LEVELS = ("High", "Medium", "Low")

# Evidence is grouped into these fixed buckets in the output (RULE 11).
EVIDENCE_CATEGORIES = ("Code", "Git", "Logs", "Tests", "Configuration", "Ticket")

# Categorical confidence → internal numeric, used ONLY for the existing delivery
# gate (RCA_CONFIDENCE_THRESHOLD) and fix gate (RCA_CONFIDENCE_THRESHOLD_FOR_FIX).
# It is never shown to the user (RULE 6 — no fake probabilities).
_CONFIDENCE_NUMERIC = {"High": 0.95, "Medium": 0.75, "Low": 0.3}

INSUFFICIENT_DATA_MESSAGE = (
    "INSUFFICIENT DATA: Target code or evidence required for RCA is unavailable. "
    "Root cause cannot be determined."
)

NO_FIX_MESSAGE = "Diagnosis only. Insufficient evidence to recommend a safe fix."

# How much of the investigation trace to hand synthesis. The trace holds the
# read_file/grep observations that become the cited EVIDENCE, so these caps are
# deliberately generous — an under-fed trace makes the model unable to cite
# file:line and collapse to "insufficient data" with empty evidence.
_MAX_TRACE_ENTRIES = 40      # most recent tool observations to include
_MAX_OBS_CHARS = 4000        # per-observation char budget (fits a small file read)
_MAX_TRACE_CHARS = 28000     # overall trace budget in the synthesis prompt

_SYSTEM = """\
You are an Enterprise Software Root Cause Analysis (RCA) Engine.

Identify the most probable root cause of a software defect using ONLY verifiable
evidence from source code, git history, logs, stack traces, test failures,
configuration, deployment artifacts, and the ticket itself.

Never optimize for producing an RCA. Optimize for being CORRECT.

You are given the ticket, extracted signals, retrieved candidate code locations,
and a read-only investigation summary and trace containing the evidence actually
gathered.

Produce ONLY a JSON object. No prose. No markdown fences. Use EXACTLY these keys:

{
  "insufficient_data": bool,
  "issue_classification": str,
  "confidence": "High"|"Medium"|"Low",
  "facts": [str, ...],
  "inferences": [str, ...],
  "unknowns": [str, ...],
  "root_cause": str,
  "root_cause_confirmed": bool,
  "root_cause_location": {
    "repo": str|null,
    "file": str|null,
    "symbol": str|null,
    "lines": str|null
  }|null,
  "introduced_by": {
    "name": str|null,
    "commit": str|null,
    "date": str|null
  }|null,
  "evidence": [
    {
      "category": "Code"|"Git"|"Logs"|"Tests"|"Configuration"|"Ticket",
      "detail": str,
      "ref": str
    }
  ],
  "contributing_factors": [str, ...],
  "additional_evidence_required": [str, ...],
  "verification_steps": [str, ...],
  "recommended_fix": str|null
}

Classification: choose EXACTLY one:

- Runtime Bug
- Missing Implementation
- Configuration Issue
- Infrastructure Issue
- Deployment Issue
- Data Issue
- Requirement Gap
- Third-party Dependency
- Performance Issue
- Security Issue
- Cannot Determine


HARD RULES

1. EVIDENCE FIRST

Every conclusion must be directly supported by evidence you can point to.

If you cannot point to evidence, do not state it.

Never infer infrastructure failures, deployment issues, architecture problems,
developer mistakes, QA failures, code-review failures, or process gaps without
direct evidence.


2. NO GHOST DATA

Set insufficient_data=true ONLY when missing evidence prevents identification of
any evidence-backed root-cause candidate.

The unavailability of one source, such as a PR, log, repository, test, or deployment
artifact, does NOT by itself require insufficient_data=true if other available
evidence is sufficient to identify and support a root-cause candidate.

When insufficient_data=true:

- root_cause must be exactly "Undetermined."
- root_cause_confirmed must be false.
- confidence must be "Low".
- issue_classification must be "Cannot Determine".
- recommended_fix must be null.
- verification_steps must be [].
- additional_evidence_required must identify the specific evidence needed.

Do not speculate.


3. CLASSIFY FIRST

Classify the primary CAUSE, not merely the symptom.

A feature that was never implemented is NOT a Runtime Bug. Use Missing
Implementation.

Use the following precedence rules:

- Security Issue: the primary mechanism or impact is a security vulnerability.
- Performance Issue: correctness is intact, but latency, throughput, memory, CPU,
  or resource consumption is defective.
- Data Issue: incorrect, missing, duplicated, stale, or corrupted persisted data
  is the primary cause.
- Configuration Issue: application code is correct, but a runtime setting, feature
  flag, secret, mapping, environment variable, or parameter is wrong.
- Deployment Issue: the correct code or configuration exists, but the wrong
  version, artifact, migration, rollout state, or deployment package is active.
- Infrastructure Issue: the primary cause is failure of underlying compute,
  network, storage, broker, DNS, database infrastructure, or platform services.
- Third-party Dependency: the primary cause is an external service or library
  outside the organisation's control.
- Missing Implementation: required behaviour was never implemented.
- Requirement Gap: the required behaviour cannot be determined because the
  specification itself is missing, contradictory, or materially ambiguous.
- Runtime Bug: fallback for defective application logic executing at runtime.
- Cannot Determine: no evidence-backed primary cause can be established.

Choose exactly one primary classification.

If a ticket contains multiple observed symptoms, first determine whether they
share the same causal mechanism.

If they share one causal mechanism, analyze them together.

If they require different causal mechanisms, treat them as separate defects.
Never force independent defects into one root cause merely because they appear
in the same ticket.

If the output schema permits only one root cause, analyze only the primary defect.
Do not include independent secondary defects in root_cause, contributing_factors,
or recommended_fix. They may remain in facts only if directly observed and
material to understanding the ticket.


4. FACTS ARE NOT INFERENCES

facts contains ONLY directly observed evidence.

Examples:
- exact code behaviour;
- exact log output;
- exact stack trace;
- exact HTTP response;
- exact test result;
- exact configuration value;
- exact ticket content;
- exact git commit or diff.

inferences contains ONLY logical conclusions supported by one or more facts.

unknowns contains evidence that is genuinely missing and materially relevant.

Do not place assumptions in facts.


5. ROOT CAUSE VERIFICATION

A root cause is CONFIRMED only when ALL THREE conditions hold:

(a) It explains every observed symptom attributed to that root cause.

Do not require one root cause to explain symptoms that have been identified as
separate, independent defects.

(b) It is supported by sufficient direct evidence.

Prefer at least two independent evidence sources.

One source is sufficient ONLY when it directly demonstrates the complete causal
mechanism and no relevant implementation surface, runtime dependency, or
alternative execution path remains unexamined that could plausibly change the
conclusion.

Examples may include:
- a deterministic failing test that isolates and proves the defective logic;
- a stack trace pointing to the exact defective line where the failure mechanism
  is unambiguous;
- direct code evidence that unambiguously reproduces the reported behaviour.

Zero search results, absence of a code match, or failure to locate an identifier
are NEVER conclusive by themselves.

Absence of evidence is not evidence of absence. Failure to find code,
configuration, routes, procedures, or identifiers proves only that they were not
found within the searched scope. It does not prove that the implementation does
not exist elsewhere.

A Missing Implementation classification can be confirmed only when either:
- all relevant implementation surfaces have been searched with sufficient
  coverage; or
- independent evidence confirms that the required implementation was never
  created.

(c) No contradictory evidence or material unresolved unknown exists.

Before setting root_cause_confirmed=true, evaluate every material unknown against
the leading hypothesis.

If any unresolved unknown could plausibly invalidate, contradict, or provide a
materially different explanation for the proposed root cause, the root cause is
not confirmed.

If all three conditions hold:
- set root_cause_confirmed=true.

If a leading evidence-backed candidate exists but any condition fails:
- keep the candidate in root_cause;
- set root_cause_confirmed=false;
- set confidence="Medium";
- list exactly what is required to confirm or reject it in
  additional_evidence_required.

If no evidence-backed candidate exists:
- set root_cause exactly to "Undetermined.";
- set root_cause_confirmed=false;
- set confidence="Low".


6. CONFIDENCE IS DETERMINISTIC

Derive confidence directly from root-cause status:

- High: root_cause_confirmed=true.
- Medium: root_cause_confirmed=false, but root_cause contains a leading
  evidence-backed candidate.
- Low: root_cause="Undetermined." or insufficient_data=true.

Never output a numeric probability or percentage.


7. ROOT-CAUSE LOCATION

Populate root_cause_location only with locations directly supported by evidence.

Never invent a repo, file, symbol, or line number.

If root_cause="Undetermined.", set root_cause_location=null unless a specific
suspected location is directly supported by evidence.

A suspected location does not by itself confirm the root cause.


8. DYNAMIC SIZE

Match depth to the defect.

A trivial UI/CSS bug should normally have:
- 1-2 facts;
- a one-sentence root cause;
- only directly relevant evidence.

A complex P1/P2 production, backend, database, infrastructure, security, or
financial-flow incident may require deeper analysis.

Do not pad.


9. ROOT CAUSE ONLY

The following are NOT root causes by themselves:

- missing tests;
- "QA missed it";
- "code review missed it";
- missing documentation;
- "developer error";
- process gaps.

Include them only under contributing_factors and only when directly supported by
evidence.

Otherwise set contributing_factors to [].


10. GIT BLAME

git_blame identifies who last modified a line.

It does NOT prove who introduced the defect or who is responsible for it.

git_blame may be used as a starting point for authorship investigation, but never
populate introduced_by from git_blame alone.

Use git_log and the relevant commit diff, or equivalent git-history evidence, to
determine whether a specific commit actually introduced the defective logic.


11. AUTHORSHIP

Populate introduced_by ONLY when git history identifies the specific commit that
introduced the root-cause code or defective behaviour.

The evidence must include:
- the introducing commit; and
- verification from its diff or equivalent git history that it introduced the
  defective logic.

Do NOT use the last-modifying commit as a substitute for the introducing commit.

Reporting authorship is factual metadata. It must never be presented as proof of
fault, negligence, or responsibility.

If the introducing commit cannot be determined, set introduced_by=null.


12. EVIDENCE CATEGORIES

Every evidence item must use exactly one category:

- Code
- Git
- Logs
- Tests
- Configuration
- Ticket

Put each item in its single best-fitting category.

Every evidence item must have:
- a precise detail; and
- a precise ref, such as file:line, commit hash, log line, test name, config key,
  or ticket field.

Do not duplicate the same evidence across categories.


13. NEXT ACTION MATCHES CONFIDENCE

If confidence="High":
- recommended_fix may contain the exact advisory code or configuration change;
- verification_steps should be [] unless post-fix verification is explicitly
  necessary and directly specific to this defect.

If confidence="Medium":
- recommended_fix must be null;
- verification_steps must contain only the specific checks required before any
  code or configuration change.

If confidence="Low":
- recommended_fix must be null;
- verification_steps must be [];
- additional_evidence_required must identify the specific missing evidence needed.


14. NO GENERIC RECOMMENDATIONS

recommended_fix, verification_steps, and additional_evidence_required must directly
address THIS defect.

Never say:
- improve QA;
- improve testing;
- improve reviews;
- improve documentation;
- add monitoring;

unless evidence demonstrates that the specific failure directly requires that
action.


15. RECOMMENDED FIX

recommended_fix is non-null ONLY when ALL THREE conditions hold:

- confidence="High";
- root_cause_confirmed=true;
- root_cause is not "Undetermined."

The fix must directly address the confirmed root cause.

It is an advisory proposal for a human. It is never applied automatically.


16. OUTPUT COMPLETENESS

Every key in the JSON schema is mandatory.

Use:
- [] for an empty array;
- null where the schema permits null.

Do not omit keys.

Do not add keys.


FINAL PRINCIPLE

It is better to return root_cause="Undetermined." than an incorrect one.

Never reward completeness over correctness.
"""

_SCHEMA_KEYS = (
    "insufficient_data", "issue_classification", "confidence", "facts",
    "inferences", "unknowns", "root_cause", "root_cause_confirmed",
    "root_cause_location", "introduced_by", "evidence", "contributing_factors",
    "additional_evidence_required", "verification_steps", "recommended_fix",
)


def _truncate_observation(observation: Any, limit: int) -> Any:
    """Return the observation compactly: the object if small, else a truncated
    string. Never produces invalid JSON (no slicing of serialized text)."""
    try:
        dumped = json.dumps(observation)
    except (TypeError, ValueError):
        return str(observation)[:limit]
    if len(dumped) <= limit:
        return observation
    return dumped[:limit] + "…(truncated)"


def _user_message(ticket: dict[str, Any], extracted: dict[str, Any],
                  candidates: list[dict[str, Any]], investigation: str,
                  trace: list[dict[str, Any]]) -> str:
    # The trace is the evidence synthesis cites in EVIDENCE — starving it makes the
    # model unable to point to file:line and fall back to "none available". Keep a
    # generous window: the read_file / grep observations carry the actual proof.
    # Truncate each observation as a STRING (slicing serialized JSON and re-parsing
    # would be invalid); keep it a plain string so the outer dumps stays valid.
    trimmed = [
        {"tool": t.get("tool"), "input": t.get("input"),
         "observation": _truncate_observation(t.get("observation"), _MAX_OBS_CHARS)}
        for t in trace[-_MAX_TRACE_ENTRIES:]
    ]
    return (
        "TICKET\n" + json.dumps(ticket, indent=2)[:4000] + "\n\n"
        "EXTRACTED SIGNALS\n" + json.dumps(extracted, indent=2) + "\n\n"
        "RETRIEVAL CANDIDATES\n" + json.dumps(candidates, indent=2)[:4000] + "\n\n"
        "INVESTIGATION SUMMARY (read-only agent)\n" + (investigation or "(none)") + "\n\n"
        "INVESTIGATION TRACE (recent tool observations)\n"
        + json.dumps(trimmed, indent=2)[:_MAX_TRACE_CHARS]
    )


def synthesize(
    settings: Settings,
    *,
    ticket: dict[str, Any],
    extracted: dict[str, Any],
    candidates: list[dict[str, Any]],
    investigation: str,
    trace: list[dict[str, Any]],
    llm_client: Any = None,
) -> dict[str, Any]:
    """Produce the structured diagnosis dict. Injectable client for tests."""
    if llm_client is None:
        from app.llm_client import build_llm_client
        llm_client = build_llm_client(
            settings,
            timeout_override=settings.llm_test_case_timeout_seconds,
        )

    raw = llm_client.complete(
        _SYSTEM,
        _user_message(ticket, extracted, candidates, investigation, trace),
        # Generous headroom: reasoning models (GPT-5) spend part of the completion
        # budget on internal reasoning, so a tight cap can truncate the JSON.
        max_tokens=8000,
    )
    try:
        parsed = parse_model_json(raw)
    except Exception as exc:
        log.warning("RCA synthesis JSON parse failed: %s", exc)
        parsed = {}

    return _normalize(parsed, settings)


def _with_model(settings: Settings, model: str) -> Settings:
    import dataclasses
    return dataclasses.replace(settings, llm_model=model)


def _normalize(parsed: dict[str, Any], settings: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {k: parsed.get(k) for k in _SCHEMA_KEYS}

    # Location is kept for linking/blame; it is not asserted as the root cause.
    rc = out.get("root_cause_location") or {}
    out["root_cause_location"] = {
        "repo": str(rc.get("repo") or ""), "file": str(rc.get("file") or ""),
        "symbol": rc.get("symbol"), "lines": rc.get("lines"),
    }

    out["issue_classification"] = _one_of(
        out.get("issue_classification"), ISSUE_CLASSES, "Cannot Determine")

    out["facts"] = _str_list(out.get("facts"))
    out["inferences"] = _str_list(out.get("inferences"))
    out["unknowns"] = _str_list(out.get("unknowns"))
    out["contributing_factors"] = _str_list(out.get("contributing_factors"))
    out["additional_evidence_required"] = _str_list(out.get("additional_evidence_required"))
    out["verification_steps"] = _str_list(out.get("verification_steps"))
    out["evidence"] = _evidence_list(out.get("evidence"))
    out["introduced_by"] = _introduced_by(out.get("introduced_by"))
    out["root_cause"] = str(out.get("root_cause") or "").strip()
    confirmed = bool(out.get("root_cause_confirmed"))

    insufficient = bool(out.get("insufficient_data"))

    # Determine the root-cause status. RULE 5 lets a single conclusive source
    # confirm, so we no longer force Undetermined at <2 evidence — the model owns
    # that call. We keep a floor: a stated cause needs at least one evidence item.
    if insufficient:
        # RULE 2 — insufficient data: a fixed, non-speculative shape.
        out["root_cause"] = INSUFFICIENT_DATA_MESSAGE
        out["issue_classification"] = "Cannot Determine"
        confirmed, status = False, "insufficient"
    else:
        undetermined = (not out["root_cause"]
                        or out["root_cause"].lower().startswith("undetermined"))
        if undetermined or not out["evidence"]:
            out["root_cause"] = "Undetermined."
            confirmed, status = False, "undetermined"
        elif confirmed:
            status = "confirmed"
        else:
            status = "most_likely"

    # RULE 6 — confidence is DETERMINISTIC from status; the model's label is not
    # trusted directly.
    label = {"confirmed": "High", "most_likely": "Medium",
             "undetermined": "Low", "insufficient": "Low"}[status]

    out["root_cause_confirmed"] = confirmed
    out["root_cause_status"] = status
    out["confidence_label"] = label
    out["confidence"] = _CONFIDENCE_NUMERIC[label]  # internal gate only

    # Locations: RULE 7 permits null; keep a normalized dict only when populated.
    if not any(out["root_cause_location"].get(k) for k in ("repo", "file", "symbol", "lines")):
        out["root_cause_location"] = None

    # RULES 13-15 — surface exactly the one NEXT ACTION for the tier.
    fix = out.get("recommended_fix")
    if status == "confirmed" and isinstance(fix, str) and fix.strip():
        out["recommended_fix"] = fix.strip()      # RULE 15 (High + confirmed only)
    else:
        out["recommended_fix"] = None
    if label != "Medium":
        out["verification_steps"] = []            # verification is the Medium action
    if status == "confirmed":
        out["additional_evidence_required"] = []  # nothing left to confirm
    return out


def _introduced_by(value: Any) -> Optional[dict[str, Optional[str]]]:
    """Normalize authorship to {name, commit, date} or None (RULE 10). Only a
    real commit reference counts; a bare name with no commit is dropped so we
    never present unsupported authorship."""
    if not isinstance(value, dict):
        return None
    commit = str(value.get("commit") or "").strip()
    if not commit:
        return None
    name = str(value.get("name") or "").strip()
    date = str(value.get("date") or "").strip()
    return {"name": name or None, "commit": commit, "date": date or None}


def _one_of(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    for candidate in allowed:
        if text.lower() == candidate.lower():
            return candidate
    return default


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _evidence_list(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(value, list):
        for e in value:
            if isinstance(e, dict):
                ref = str(e.get("ref") or "")
                out.append({
                    "category": _evidence_category(
                        e.get("category") or e.get("type"), ref),
                    "detail": str(e.get("detail") or ""),
                    "ref": ref,
                })
            elif isinstance(e, str) and e.strip():
                out.append({"category": _evidence_category(None, ""),
                            "detail": e.strip(), "ref": ""})
    return out


# Legacy `type` keywords → the fixed category buckets (RULE 11). Anything that
# doesn't match a non-code bucket is treated as Code.
_CATEGORY_KEYWORDS = (
    ("Git", ("git", "commit", "blame", "history", "diff", "pr ", "pull")),
    ("Logs", ("log",)),
    ("Tests", ("test", "spec", "assert")),
    ("Configuration", ("config", "env", "setting", "yaml", ".env")),
    ("Ticket", ("ticket", "repro", "jira", "comment")),
)


def _evidence_category(raw: Any, ref: str) -> str:
    text = str(raw or "").strip()
    for category in EVIDENCE_CATEGORIES:
        if text.lower() == category.lower():
            return category  # explicit, valid category
    hay = f"{text} {ref}".lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(k in hay for k in keywords):
            return category
    return "Code"
