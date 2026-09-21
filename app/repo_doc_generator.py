"""Generate repository documentation (onboarding guide, architecture overview).

The document is grounded in the same RepoTree artifacts the test-case pipeline
uses: the per-repo architecture map (`per_repo/<name>.md`) and the Repomix packed
source (`packed/<name>.xml`). Both are fed to the in-process LLM with a
document-specific prompt, so the output cites real files, routes, tables, env keys,
and dependencies instead of inventing them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable, Optional

from app.repo_tree_integration import repo_tree_runtime

log = logging.getLogger(__name__)

# How much packed-source context to feed the model. ~450K chars ≈ ~110K tokens,
# comfortably inside the model context window alongside the arch map and prompt.
_PACKED_CONTEXT_MAX_CHARS = 450_000
_DIRECTORY_PREVIEW_MAX_CHARS = 24_000
_DOC_MAX_OUTPUT_TOKENS = 16_000


# ============================================================
# Document type registry
# ============================================================

# This repo emits FOUR documents. To keep a single source of truth and avoid
# cross-document duplication, every concern is owned by exactly ONE document; the
# other documents reference it instead of reproducing its tables.
_DOC_FAMILY_MAP = dedent("""
    This repository has FOUR separate generated documents, each with a single,
    non-overlapping responsibility. Include ONLY what THIS document owns. For anything
    owned by another document, point the reader to it in one short line — never
    reproduce another document's tables or lists.

    Ownership map (single source of truth per concern):
    - Onboarding Guide — purpose, local setup, migrations, modules/packages inventory,
      route/endpoint catalog, schedulers & queues, database tables, major features,
      external integrations, setup inconsistencies & resolutions, feature entry
      points, troubleshooting.
    - Architecture — runtime/structural design only: summary dashboard, system
      overview, mermaid diagrams, module dependency map & runtime boundaries, request
      lifecycle, deployment architecture, configuration architecture, data flow,
      cross-layer dependency violations, data ownership, constraints & trade-offs.
    - Engineering Scorecard — executive scoring only: weighted parameter scores,
      overall score, repo quality signals, risk heatmap, top strengths/weaknesses,
      readiness summaries, prioritized findings.
    - Technical Audit — deep code-health & remediation: complexity, coupling,
      blast-radius, high-risk module inventory, dependency hygiene, dead code,
      CI/testing/security/performance/DX reviews, technical debt, recommendations.
""").strip()


_ONBOARDING_SYSTEM = dedent(f"""
    {_DOC_FAMILY_MAP}

    YOUR DOCUMENT: Onboarding Guide. Stay strictly in scope. Do NOT include
    architecture diagrams or runtime-boundary maps (Architecture owns those),
    code-health metrics such as complexity/coupling/blast-radius/centrality (Technical
    Audit owns those), or quality scores/heatmaps (Engineering Scorecard owns those) —
    reference those documents in one line instead.

    You are a senior staff engineer writing an authoritative onboarding guide for a
    repository that a brand-new developer has never seen. You work ONLY from the
    repository context provided (its architecture map and packed source). Every fact
    must be grounded in a real file, route, table, env key, dependency, or config
    value visible in that context. Do not invent endpoints, tables, env vars, or
    behaviour. If something is ambiguous or missing, say so explicitly rather than
    guessing.

    While reading, actively hunt for INCONSISTENCIES and record them (these are high
    value to a new dev): dependencies installed but never instantiated; routes or
    cron jobs that are commented out; scripts that reference files (docker-compose,
    migrations, .env.example) that do not exist; route files present but not mounted;
    two competing approaches for the same concern.

    OUTPUT: Markdown only. Start with:

        # Onboarding Guide - <repo name>

        **Repository:** `<repo name>`
        **Updated:** <today's date YYYY-MM-DD>

    Then produce these sections IN THIS ORDER. Adapt section names to the repo's
    actual stack, but keep the intent and the table-driven style. Prefer tables over
    prose. Keep entries concrete and short.

    1. ## Purpose — 2-4 sentences: what this service owns in business terms, and
       whether it is a simple CRUD API or a stateful workflow engine.
    2. ## Specific Modules And Packages — table | Area | Module or package | Purpose |
       citing real package names AND real file paths.
    3. ## Local Setup — numbered steps (runtime version, install, .env creation —
       note if no committed `.env.example` exists, run command, health-check) plus a
       table | Dependency | Required for | Important env keys | using REAL env names.
    4. ## Setup Inconsistencies And Resolution — table | Inconsistency | Where it
       appears | Impact | Recommended resolution |. If genuinely none, say so.
    5. ## Running Migrations — real migration commands and where migrations live;
       note if the directory is empty/absent.
    6. Active Route Modules — table of only the modules actually mounted/registered.
    7. ## Request Format Conventions — auth/content-type conventions per client type,
       with short `http` snippets.
    8. Public surface — list EVERY entry in the repo's public surface, grouped into
       `###` subsections, each a table | Method | Endpoint | Sample format |
       High-level purpose |. For an HTTP API these are REST endpoints; for a frontend
       use routes/pages; for a CLI use commands; for a library use exported API. Add a
       `### Not Currently Mounted` table for files/blocks that exist but are commented
       out or unmounted.
    9. ## Application Schedulers And Queues — table | Scheduler or queue | Code
       location | Current state | What it does |. Distinguish active vs installed-
       but-unused.
    10. ## Database Tables And Collections — table grouped BY PURPOSE |
        Purpose | Tables or collections | Notes | using real model/table names.
    11. ## Major Features And Working Description — table | Feature | Primary modules
        | How it works | naming real route/controller/service/table files.
    12. ## Other Services And Integrations — table | Service | Module/config | Used
        for | Recommendation | for every external/sister-service dependency.
    13. ## Feature Entry Points — table | Task | Start here | with concrete file paths.
    14. ## Troubleshooting — table | Symptom | Check | tying failures back to the
        specific files/env/services and to the inconsistencies found.

    Reference env KEY NAMES only, never values. Be exhaustive on endpoints and
    tables; be terse in wording. If a section does not apply to this repo's stack,
    replace it with the closest equivalent and note why. Choose one concrete `##`
    heading per section that fits the stack; never copy the parenthetical guidance
    above literally into a heading.
""").strip()


_METRIC_RULE = dedent("""
    Metrics rule: derive every metric only from the provided context. File counts,
    dominant file types, approximate line counts, and largest/most-central files can be
    computed from the packed source. Git-only signals (commit count, last commit date,
    branch, churn history) are usually NOT in static context — when a value is unknown,
    write "not available from static context" instead of inventing a number.
""").strip()


_ARCHITECTURE_SYSTEM = dedent(f"""
    {_DOC_FAMILY_MAP}

    YOUR DOCUMENT: Architecture. Cover runtime/structural design ONLY. Do NOT include
    setup/run instructions, the full endpoint catalog, the full package or database-
    table inventory (the Onboarding Guide owns those), code-health metrics such as
    complexity/coupling/blast-radius/centrality (Technical Audit owns those), or
    quality scores (Engineering Scorecard owns those). Where structure references those
    things, point to the owning document in one line instead of reproducing it.

    You are a principal engineer writing the ARCHITECTURE document for a repository.
    Work ONLY from the provided repository context (architecture map and packed
    source). Ground every statement in real files, modules, routes, tables, or
    dependencies. Do not invent anything; flag gaps explicitly. Read this as runtime
    boundaries and ownership maps, not as a quality score.

    {_METRIC_RULE}

    OUTPUT: Markdown only. Start with:

        # Architecture - <repo name>

        **Repository:** `<repo name>`
        **Updated:** <today's date YYYY-MM-DD>
        **System type:** <one line, e.g. Node.js onboarding API service>

    Then produce these sections IN THIS ORDER. Prefer tables; keep wording terse.

    1. ## Architecture Summary Dashboard — table | Attribute | Value | (primary stack,
       runtime boundary, repository size in files/lines, dominant file types,
       deployment path, git branch if known). One-line stack/size only — not an inventory.
    2. ## System Overview — what it does; then **Entry points** and **Data and state**
       bullet lists citing real files/datastores.
    3. ## Architecture Diagrams — a ```mermaid``` flowchart of the high-level components.
    4. ## Module Dependency Map — table | Module | Files | Responsibility | focused on
       how modules depend on / relate to each other (not a package inventory).
    5. ## Runtime Boundaries — table | Boundary | Inside | Outside |.
    6. ## Request Lifecycle — numbered end-to-end flow (entry → validation → domain →
       data/providers → response; async/deploy side effects isolated).
    7. ## Deployment Architecture — build/deploy path and key release checks.
    8. ## Configuration Architecture — how config/secrets are owned (env, config files,
       secret stores); name the real config sources (env KEY names only).
    9. ## Data Flow — a ```mermaid``` sequence diagram of a representative request.
    10. ## Cross-Layer Dependency Violations — table | Pattern to avoid | Why |.
    11. ## Data Ownership Boundaries — per datastore, which layer/module owns it and the
        parity/change rule (do NOT re-list every table — that lives in the Onboarding
        Guide).
    12. ## Constraints & Trade-offs — bullet list of guardrails.

    For anything outside this scope (full endpoint list, all tables, code-health
    metrics, scores), add a one-line pointer such as "See the Onboarding Guide" or
    "See the Technical Audit" instead of a table.

    Reference env KEY NAMES only, never values. Be concrete and grounded.
""").strip()


_SCORECARD_SYSTEM = dedent(f"""
    {_DOC_FAMILY_MAP}

    YOUR DOCUMENT: Engineering Scorecard. Produce scoring and executive signals ONLY.
    Do NOT include setup steps, endpoint/table/module inventories, architecture
    diagrams, or detailed remediation walkthroughs — those live in the Onboarding
    Guide, Architecture, and Technical Audit. Keep findings to one-line signals and
    point to the Technical Audit for detail.

    You are a principal engineer producing an executive-readable ENGINEERING SCORECARD
    for a repository. Work ONLY from the provided repository context (architecture map
    and packed source). Be candid but calibrated, and keep it high-level: this is a
    readiness signal, not an implementation walkthrough. Do not invent facts.

    {_METRIC_RULE}

    Scoring: score each parameter out of 10 from the evidence visible in the context,
    then compute the weighted Overall score out of 100 using the exact weights below.
    Calibrate honestly — absent tests, secrets in source, or god-object files should pull
    scores down; clear boundaries and observability should pull them up.

    OUTPUT: Markdown only. Start with:

        # Engineering Scorecard - <repo name>

        **Repository:** `<repo name>`
        **Updated:** <today's date YYYY-MM-DD>

    Then produce these sections IN THIS ORDER:

    1. ## Parameter-Wise Summary — table | Parameter | Weight | Score | Short
       observation | with EXACTLY these parameters and weights: Architecture 15%,
       Code quality 15%, Testing 15%, Security 20%, DevOps / CI-CD 10%,
       Performance 10%, Documentation 5%, Developer experience 5%,
       Engineering practices 5%.
    2. ## Overall Engineering Score — **N / 100** (the weighted total), then one line
       stating it is an executive readiness signal.
    3. ## Repo Quality Signals — table | Signal | Value | (primary stack, files/lines,
       branch if known, last commit if known, build/deploy path).
    4. ## Risk Heatmap — table | Risk area | Likelihood | Impact | Priority |.
    5. ## Top 5 Strengths — numbered list.
    6. ## Top 5 Weaknesses — numbered list.
    7. ## Production Readiness Summary — short paragraph.
    8. ## Scale Readiness Summary — short paragraph.
    9. ## Prioritized Findings — table | Priority | Finding | Impact | (P0..P3).
    10. ## Recommended Priority Fixes — numbered list of concrete fixes.

    Reference env KEY NAMES only, never values.
""").strip()


_AUDIT_SYSTEM = dedent(f"""
    {_DOC_FAMILY_MAP}

    YOUR DOCUMENT: Technical Audit. Cover code-health and remediation ONLY. Do NOT
    include getting-started/setup, business-feature descriptions, the full endpoint/
    table inventory, architecture diagrams, or the scored rubric (the Engineering
    Scorecard owns scoring). You DO own the detailed metrics — complexity, coupling,
    blast-radius, high-risk inventory, dependency hygiene, dead code. Reference the
    other documents in one line where needed.

    You are a principal engineer producing a TECHNICAL AUDIT of a repository for
    engineering leadership. Work ONLY from the provided repository context
    (architecture map and packed source). Be specific and grounded; cite real files.
    Do not invent facts or metrics.

    {_METRIC_RULE}

    OUTPUT: Markdown only. Start with:

        # Technical Audit - <repo name>

        **Repository:** `<repo name>`
        **Updated:** <today's date YYYY-MM-DD>

    Then produce these sections IN THIS ORDER. Prefer tables; keep wording terse.

    1. ## Repo Snapshot Dashboard — table | Signal | Observed state | (stack,
       files/lines, branch & commit info if known, largest risk surface files).
    2. ## Executive Summary — 2-4 sentences on overall state and the safest next step.
    3. ## Repository Quality Assessment — table | Area | Assessment |.
    4. ## Folder Analysis — table | Folder/module | Purpose | Audit note |.
    5. ## Dependency Analysis — table | Group | Observed packages | (Runtime vs
       Development, from the real manifest in context) + a one-line hygiene focus.
    6. ## Codebase Metrics — table | Metric | Value | (scanned lines, files, dominant
       file types — derived from packed source).
    7. ## Complexity Analysis — `###` subsection: table | File | Lines | Why it matters |
       for the largest files (line counts from packed source).
    8. ## Coupling & Blast-Radius — `###` subsections covering coupling risk, blast
       radius, high-risk module inventory, and change-risk mapping (tables where useful).
    9. ## CI/CD Review — build/deploy path and recommended CI gates.
    10. ## Testing Review — current state and the highest-value tests to add first.
    11. ## Security Review — secrets, PII/financial data in logs, auth/webhook/provider
        boundaries (name the real modules).
    12. ## Performance Review — largest files, expensive provider calls, worker/bundle risk.
    13. ## DX Review — what would most improve developer experience.
    14. ## Technical Debt Assessment — table | Priority | Debt item | Action | (P0..P3).
    15. ## Recommendations — numbered list of concrete next steps.

    Reference env KEY NAMES only, never values.
""").strip()


_DOC_TYPES: dict[str, dict[str, Any]] = {
    "onboarding_guide": {
        "label": "Onboarding Guide",
        "system": _ONBOARDING_SYSTEM,
        "filename": "ONBOARDING_GUIDE",
    },
    "architecture": {
        "label": "Architecture",
        "system": _ARCHITECTURE_SYSTEM,
        "filename": "ARCHITECTURE",
    },
    "engineering_scorecard": {
        "label": "Engineering Scorecard",
        "system": _SCORECARD_SYSTEM,
        "filename": "ENGINEERING_SCORECARD",
    },
    "technical_audit": {
        "label": "Technical Audit",
        "system": _AUDIT_SYSTEM,
        "filename": "TECHNICAL_AUDIT",
    },
}


def list_document_types() -> list[dict[str, str]]:
    return [{"id": key, "label": spec["label"]} for key, spec in _DOC_TYPES.items()]


def document_type_ids() -> list[str]:
    return list(_DOC_TYPES.keys())


def document_label(doc_type: str) -> str:
    spec = _DOC_TYPES.get(doc_type)
    return spec["label"] if spec else doc_type


# ============================================================
# Repo discovery (only repos with usable RepoTree artifacts)
# ============================================================

def list_doc_repositories() -> list[dict[str, Any]]:
    """Return configured repos that have an architecture map and/or packed source."""
    state = repo_tree_runtime()
    cfg = state.config
    repos: list[dict[str, Any]] = []
    for repo in cfg.repos:
        arch = cfg.per_repo_dir / f"{repo.name}.md"
        packed = cfg.packed_dir / f"{repo.name}.xml"
        has_arch = arch.exists()
        has_packed = packed.exists()
        repos.append(
            {
                "name": repo.name,
                "description": repo.description or "",
                "has_architecture_map": has_arch,
                "has_packed_source": has_packed,
                "ready": True,
            }
        )
    repos.sort(key=lambda r: (not r["has_packed_source"], r["name"].lower()))
    return repos


# ============================================================
# Context building from RepoTree artifacts
# ============================================================

def _load_architecture_map(per_repo_dir: Path, name: str) -> str:
    f = per_repo_dir / f"{name}.md"
    if not f.exists():
        return ""
    try:
        return f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _load_packed_context(packed_dir: Path, name: str, *, max_chars: int) -> str:
    """Build a context blob from a Repomix packed XML: directory structure followed
    by as many full file blocks as fit within `max_chars`."""
    packed = packed_dir / f"{name}.xml"
    if not packed.exists():
        return ""
    try:
        text = packed.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    parts: list[str] = []
    budget = max_chars

    dir_match = re.search(r"<directory_structure>\s*(.*?)\s*</directory_structure>", text, re.S)
    if dir_match:
        structure = dir_match.group(1).strip()[:_DIRECTORY_PREVIEW_MAX_CHARS]
        block = f"## Directory Structure\n```text\n{structure}\n```"
        parts.append(block)
        budget -= len(block)

    parts.append("## Source Files")
    for match in re.finditer(r'<file path="([^"]+)">(.*?)</file>', text, re.S):
        if budget <= 0:
            parts.append("\n_(remaining files omitted to fit the context budget)_")
            break
        path = match.group(1)
        body = match.group(2).strip()
        block = f"\n### {path}\n```\n{body}\n```"
        if len(block) > budget:
            block = block[:budget] + "\n```\n_(file truncated)_"
            budget = 0
        else:
            budget -= len(block)
        parts.append(block)

    return "\n".join(parts)


def _build_repo_context(cfg: Any, name: str) -> tuple[str, dict[str, int]]:
    arch = _load_architecture_map(cfg.per_repo_dir, name)
    packed = _load_packed_context(cfg.packed_dir, name, max_chars=_PACKED_CONTEXT_MAX_CHARS)
    sections: list[str] = []
    if arch:
        sections.append("# RepoTree Architecture Map\n" + arch)
    if packed:
        sections.append("# Repomix Packed Source\n" + packed)
    context = "\n\n".join(sections)
    stats = {
        "architecture_context_chars": len(arch),
        "packed_context_chars": len(packed),
    }
    return context, stats


# ============================================================
# Entry point
# ============================================================

@dataclass
class DocGenResult:
    filename: str
    markdown: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


def build_document_context(repo_name: str) -> tuple[str, dict[str, int]]:
    """Build (and validate) the RepoTree context blob for a repo.

    Returns (context, stats). The context is identical across all document types
    for a repo, so callers generating several docs can build it once and pass it
    to ``generate_repo_document_full``. Raises ValueError on bad input/missing
    artifacts.
    """
    state = repo_tree_runtime()
    cfg = state.config
    valid_names = {r.name for r in cfg.repos}
    if repo_name not in valid_names:
        raise ValueError(
            f"Repository '{repo_name}' is not configured in RepoTree. "
            f"Configured repos: {', '.join(sorted(valid_names))}"
        )
    context, stats = _build_repo_context(cfg, repo_name)
    if not context.strip():
        # Attempt on-demand Repomix packing
        try:
            from repo_architect.reindex import reindex_repomix

            reindex_repomix(cfg, repo_names=[repo_name], do_git_pull=False, force=True)
            context, stats = _build_repo_context(cfg, repo_name)
        except Exception as exc:
            log.warning("On-demand Repomix packaging for %s failed: %s", repo_name, exc)

    if not context.strip():
        repo_obj = next((r for r in cfg.repos if r.name == repo_name), None)
        if repo_obj and repo_obj.path and Path(repo_obj.path).exists():
            context = f"# Repository: {repo_name}\nPath: {repo_obj.path}\n"
            stats = {"architecture_context_chars": 0, "packed_context_chars": len(context)}
        else:
            raise ValueError(
                f"No architecture map or packed source found for '{repo_name}'. "
                f"Please run Repomix reindex for this repo first."
            )
    return context, stats


def generate_repo_document_full(
    repo_name: str,
    doc_type: str = "onboarding_guide",
    *,
    context: Optional[str] = None,
) -> DocGenResult:
    """Generate a document and return its text plus token usage.

    Pass ``context`` (from ``build_document_context``) to avoid rebuilding it when
    generating multiple documents for the same repo.
    """
    spec = _DOC_TYPES.get(doc_type)
    if not spec:
        raise ValueError(
            f"Unknown document type '{doc_type}'. Available: {', '.join(_DOC_TYPES)}"
        )

    if context is None:
        context, stats = build_document_context(repo_name)
    else:
        stats = {"architecture_context_chars": 0, "packed_context_chars": len(context)}

    user = dedent(f"""
        Repository name: {repo_name}
        Today's date: {date.today().isoformat()}

        Use the repository context below as your only source of truth. Produce the
        requested document now.

        ===== REPOSITORY CONTEXT =====
        {context}
    """).strip()

    state = repo_tree_runtime()
    log.info(
        "Generating %s for repo=%s (arch=%d chars, packed=%d chars)",
        doc_type, repo_name, stats["architecture_context_chars"], stats["packed_context_chars"],
    )
    result = state.llm.complete(
        system=spec["system"],
        user=user,
        cache_system=True,
        max_tokens=_DOC_MAX_OUTPUT_TOKENS,
    )
    markdown = (result.text or "").strip()
    if not markdown:
        raise RuntimeError("The model returned an empty document")

    stamp = date.today().strftime("%Y%m%d")
    filename = f"{spec['filename']}-{repo_name}-{stamp}.md"
    return DocGenResult(
        filename=filename,
        markdown=markdown,
        model=getattr(state.llm, "model", "") or "",
        input_tokens=getattr(result, "input_tokens", 0) or 0,
        output_tokens=getattr(result, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(result, "cache_read_tokens", 0) or 0,
        cache_creation_tokens=getattr(result, "cache_creation_tokens", 0) or 0,
    )


def generate_repo_document(repo_name: str, doc_type: str = "onboarding_guide") -> tuple[str, str]:
    """Backward-compatible wrapper returning (filename, markdown)."""
    res = generate_repo_document_full(repo_name, doc_type)
    return res.filename, res.markdown
