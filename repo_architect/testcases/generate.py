"""Generate test cases from a JIRA ticket + architecture context.

The LLM gets:
  - The ticket fields (summary, description, acceptance criteria, etc.)
  - The architecture maps of the auto-detected relevant repo(s)
  - The desired output format (gherkin, pytest, junit, plain)

It returns structured test cases that exercise the behavior described in
the ticket, grounded in the actual routes/models/modules of the repo —
no test cases referring to code that doesn't exist.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Iterable, List, Literal, Optional

from ..config import Config
from ..embeddings import SemanticHit, semantic_search_codebase
from ..llm import LLMClient
from .detect import detect_relevant_repos

log = logging.getLogger(__name__)

TestStyle = Literal["gherkin", "pytest", "junit", "plain"]


@dataclass
class JiraTicket:
    key: str                            # e.g., "PROJ-1234"
    summary: str
    issue_type: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    labels: List[str] = None            # type: ignore
    components: List[str] = None        # type: ignore

    def __post_init__(self):
        self.labels = self.labels or []
        self.components = self.components or []

    def as_text(self) -> str:
        """Single text blob suitable for LLM context and keyword matching."""
        parts = [f"[{self.key}] {self.summary}"]
        if self.issue_type:
            parts.append(f"\nIssue Type: {self.issue_type}")
        if self.description:
            parts.append(f"\nDescription:\n{self.description}")
        if self.acceptance_criteria:
            parts.append(f"\nAcceptance Criteria:\n{self.acceptance_criteria}")
        if self.labels:
            parts.append(f"\nLabels: {', '.join(self.labels)}")
        if self.components:
            parts.append(f"\nComponents: {', '.join(self.components)}")
        return "\n".join(parts)


@dataclass
class GeneratedTestCases:
    ticket_key: str
    grounded_repos: List[str]               # which repo maps were used as context
    style: TestStyle
    test_cases: str                         # full LLM output, ready to display
    semantic_hits_count: int = 0
    files_touched_count: int = 0
    architecture_context_chars: int = 0
    repomix_context_chars: int = 0


# ============================================================
# Prompts
# ============================================================

_BASE_RULES = dedent("""
    Rules:
    - Each test case must be traceable to the ticket: the title or description
      should reference the behavior the ticket asks for or the AC line being
      verified.
    - Ground every test in the repo's actual routes/models/modules shown in
      the architecture context. If the ticket asks for behavior the repo
      can't possibly satisfy (e.g., a route that doesn't exist), say so in a
      `## Gaps` section at the end — don't invent code.
    - How many: generate only as many test cases as this specific ticket
      genuinely needs — never pad to hit a number. Let the scope decide: a
      small or simple change may need just 2-3 cases; a broad or risky one may
      need more. Cap at a HARD MAXIMUM of 8 test cases — never exceed 8. Within
      that budget, always cover the primary happy path, the edge cases that
      actually matter for this change, and at least one reachable error/failure
      case. If fewer than 8 cases fully cover the behavior, stop there; do not
      invent low-value or redundant cases just to reach the maximum.
    - Authentication, authorization, validation, and persistence are common
      misses — include them when relevant.
    - Be concrete: real payload values, real assertions. No placeholders like
      "valid input" — say what valid input looks like.
    - Do not write ambiguous expected results. Never use "OR", "either", or
      multiple acceptable status codes inside a test case's Expected result.
      If behavior is genuinely undecided, put it in `## Open Questions` or
      `## Gaps`, not inside the test case.
    - Every test case must be grounded in visible repo context. Include source
      references such as file paths, route names, controller methods, services,
      middleware, DB tables, or Qdrant/Repomix snippets used as evidence.
    - Separate setup fixtures from request payloads. Use `DB fixtures`,
      `Auth fixtures`, or `Mock fixtures` when the test needs state.
""").strip()


_BUG_RULES = dedent("""
    Bug-ticket coverage (apply each only when it genuinely fits this bug, and
    still respect the hard maximum of 8 cases):
    - Include one reproduction test for the current failing behavior.
    - Include one regression test proving the fix remains in place.
    - Include one fix-verification positive test.
    - Include negative validation coverage for malformed/missing inputs when
      the bug involves input handling.
    - Include auth/security coverage when the endpoint is protected.
    - Include a data-boundary test when the bug involves filtering, dates,
      status flags, pagination, permissions, or persistence.
    Skip any bullet that doesn't apply rather than forcing an artificial test.
""").strip()


_STYLE_INSTRUCTIONS = {
    "gherkin": dedent("""
        Output format: Gherkin scenarios.

        ```gherkin
        Feature: <one line tied to ticket>

          Scenario: <descriptive name>
            Given <precondition>
            When <action>
            Then <observable outcome>
        ```

        Group related scenarios into Features. Use Scenario Outlines with
        Examples tables when you have multiple parameter variations.
    """).strip(),

    "pytest": dedent("""
        Output format: pytest skeletons in Python.

        - One `def test_<descriptive_name>():` per case.
        - Use comments at the top of each test to cite the ticket section
          (description line, AC bullet) it covers.
        - Mock external dependencies with `unittest.mock.patch` or pytest
          fixtures. Use the import paths from the architecture context.
        - Include `# TODO: <specific thing the implementer should fill in>`
          where data setup depends on test-environment specifics, but be
          specific about WHAT.
    """).strip(),

    "junit": dedent("""
        Output format: JUnit 5 skeletons in Java.

        - One `@Test void <descriptiveName>()` per case.
        - Use `@DisplayName` to cite the ticket section.
        - `assertThat(...)` with AssertJ or `assertEquals(...)`.
        - Mock with Mockito.
    """).strip(),

    "plain": dedent("""
        Output format: conventional QA test cases, not BDD/Gherkin.
        Generate production-ready test cases following the "How many" rule
        above — as few or as many as the ticket needs, never more than 8.
        Each item must use this exact structure:

        ### TC-01: <short descriptive title>
        - Type: Positive | Negative | Edge | Regression | Security | Reproduction
        - Priority: P0 | P1 | P2
        - API / Layer: <endpoint, controller/service, job, UI export layer, etc.>
        - Source references:
          - <file/function/table/route from the provided context>
        - DB fixtures:
          - <required rows/state, or "None">
        - Auth fixtures:
          - <token/user/role setup, or "None">
        - Preconditions: ...
        - Test data: ...
        - Steps:
          1. ...
          2. ...
        - Expected result: ...
        - Assertions:
          - <specific status, body, headers, row counts, side effects>
        - Automation notes: <Postman/pytest/Jest hint or "Manual">
        - Ticket reference: <which AC line or description sentence>

        Do not wrap the whole answer in a fenced code block.
        Do not use Feature/Scenario/Given/When/Then in this style.
        Do not include alternate expected results; choose one expected contract.
    """).strip(),
}


_QA_PERSONA = dedent("""
    You are a senior QA engineer turning a JIRA ticket into test cases that
    will actually catch regressions. You are given the ticket, Qdrant
    semantic source-file hits, and Repomix-derived context for the repo(s)
    most likely affected by it.
""").strip()

_DEV_PERSONA = dedent("""
    You are a senior developer writing test cases to verify your OWN
    implementation before it goes to code review (the ticket is at the
    "Code Review" stage). You are given the ticket, Qdrant semantic
    source-file hits, and Repomix-derived context for the repo(s) most
    likely affected by it. Focus on implementation-level verification:
    unit-test scenarios for the changed functions, the code paths a
    reviewer would want exercised, boundary/edge cases, error and failure
    handling, and the integration points visible in the changed code and
    the provided repo context. Prefer cases a developer can run themselves
    to catch defects before QA ever sees the ticket.
""").strip()


def _build_prompt(
    ticket: JiraTicket,
    arch_context: str,
    style: TestStyle,
    audience: str = "qa",
) -> tuple[str, str]:
    issue_type_rules = _BUG_RULES if ticket.issue_type.lower() == "bug" else ""
    persona = _DEV_PERSONA if audience == "dev" else _QA_PERSONA
    system = dedent(f"""
        {persona}

        {_BASE_RULES}

        {issue_type_rules}

        {_STYLE_INSTRUCTIONS[style]}
    """).strip()

    user = dedent(f"""
        === TICKET ===
        {ticket.as_text()}

        === REPOMIX / ARCHITECTURE CONTEXT ===
        {arch_context}

        Produce test cases in {style} format. Start with a 1-2 sentence summary
        of what the ticket asks for (so the reviewer can sanity-check your
        understanding), then the test cases.
    """).strip()

    return system, user


# ============================================================
# Entry point
# ============================================================

def _load_arch_context(per_repo_dir: Path, repo_names: List[str]) -> str:
    """Concatenate per-repo arch maps for the chosen repos."""
    parts: List[str] = []
    for name in repo_names:
        f = per_repo_dir / f"{name}.md"
        if not f.exists():
            parts.append(f"### {name}\n(no architecture map available)")
            continue
        parts.append(f"### {name}\n{f.read_text()}")
    return "\n\n".join(parts)


def _build_semantic_context(hits: list[SemanticHit]) -> str:
    if not hits:
        return "(no Qdrant semantic hits retrieved)"

    parts = ["## Qdrant Semantic Source Hits"]
    for hit in hits[:10]:
        score = f"{hit.score:.3f}"
        repo = hit.repo_name or hit.repo
        lang = hit.language or "text"
        parts.append(f"\n### {repo}:{hit.path} (score={score})")
        if hit.text:
            parts.append(f"```{lang.lower()}\n{hit.text}\n```")
    return "\n".join(parts)


def _load_repomix_file_context(
    packed_dir: Path,
    repo_names: list[str],
    semantic_hits: list[SemanticHit],
    *,
    file_char_limit: int,
    total_char_limit: int,
) -> str:
    """Pull targeted file snippets from Repomix packed XML files."""
    parts: list[str] = []
    seen: set[tuple[str, str]] = set()

    hits_by_repo: dict[str, list[SemanticHit]] = {}
    for hit in semantic_hits:
        repo_keys = {
            hit.repo,
            hit.repo_name,
            hit.repo.split("/")[-1],
            hit.repo_name.split("/")[-1],
        }
        for repo in repo_names:
            if repo in repo_keys or repo.split("/")[-1] in repo_keys:
                hits_by_repo.setdefault(repo, []).append(hit)
                break

    for repo in repo_names:
        packed = packed_dir / f"{repo}.xml"
        if not packed.exists():
            continue
        try:
            packed_text = packed.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        repo_hits = hits_by_repo.get(repo) or []
        if not repo_hits:
            preview = _directory_structure_preview(packed_text, limit=min(file_char_limit, 2500))
            if preview:
                parts.append(f"## Repomix Directory Preview: {repo}\n```text\n{preview}\n```")
            continue

        for hit in repo_hits:
            key = (repo, hit.path)
            if not hit.path or key in seen:
                continue
            seen.add(key)
            snippet = _extract_repomix_file(packed_text, hit.path, limit=file_char_limit)
            if not snippet:
                continue
            parts.append(f"## Repomix Packed File: {repo}/{hit.path}\n```text\n{snippet}\n```")
            if sum(len(p) for p in parts) >= total_char_limit:
                return "\n\n".join(parts)[:total_char_limit]

    return "\n\n".join(parts)[:total_char_limit]


def _directory_structure_preview(packed_text: str, *, limit: int) -> str:
    match = re.search(r"<directory_structure>\s*(.*?)\s*</directory_structure>", packed_text, re.S)
    if not match:
        return ""
    return match.group(1).strip()[:limit]


def _extract_repomix_file(packed_text: str, path: str, *, limit: int) -> str:
    marker = f'<file path="{path}">'
    start = packed_text.find(marker)
    if start < 0:
        normalized = path.replace("\\", "/")
        marker = f'<file path="{normalized}">'
        start = packed_text.find(marker)
    if start < 0:
        return ""
    content_start = start + len(marker)
    end = packed_text.find("</file>", content_start)
    if end < 0:
        return ""
    return packed_text[content_start:end].strip()[:limit]


def _unique_paths(hits: Iterable[SemanticHit]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.path and hit.path not in seen:
            seen.add(hit.path)
            paths.append(hit.path)
    return paths


def generate_test_cases(
    ticket: JiraTicket,
    cfg: Config,
    llm: LLMClient,
    style: TestStyle = "plain",
    repos_override: Optional[List[str]] = None,
    embedding_model: str = "codebase_bge_m3",
    top_k: int = 15,
    include_semantic_context: bool = True,
    audience: str = "qa",
    pr_context: Optional[str] = None,
) -> GeneratedTestCases:
    """Generate test cases for a JIRA ticket.

    Auto-detects relevant repos unless `repos_override` is provided.
    When `pr_context` is supplied (developer flow at Code Review), the PR diff is
    injected as authoritative latest code — RepoTree's indexed maps track the main
    branch and won't reflect the branch under review.
    Returns a GeneratedTestCases with the LLM's full output.
    """
    if repos_override:
        valid_names = {r.name for r in cfg.repos}
        relevant = [r for r in repos_override if r in valid_names]
        if not relevant:
            raise ValueError(
                f"None of {repos_override} match any configured repo. "
                f"Configured: {sorted(valid_names)}"
            )
        log.info("Using caller-specified repos: %s", relevant)
    else:
        relevant = detect_relevant_repos(ticket.as_text(), cfg, llm)
        if not relevant:
            raise ValueError(
                f"Could not identify any relevant repo for ticket {ticket.key}. "
                f"Either the ticket is too vague or no repo handles this area. "
                f"You can pass `repos` explicitly to bypass auto-detection."
            )
        log.info("Auto-detected relevant repos: %s", relevant)

    semantic_hits = (
        semantic_search_codebase(
            ticket.as_text(),
            cfg=cfg,
            repo_names=relevant,
            embedding_model=embedding_model,
            top_k=top_k,
        )
        if include_semantic_context
        else []
    )

    arch_maps = _load_arch_context(cfg.per_repo_dir, relevant)
    semantic_context = _build_semantic_context(semantic_hits)
    repomix_file_context = _load_repomix_file_context(
        cfg.packed_dir,
        relevant,
        semantic_hits,
        file_char_limit=cfg.repomix_file_context_chars,
        total_char_limit=cfg.repomix_context_max_chars,
    )
    context_sections: List[str] = []
    if pr_context:
        # Highest priority: the actual code under review. RepoTree's indexed maps
        # and Repomix packs track the main branch, so where they disagree with the
        # PR diff, the PR is authoritative for developer test cases.
        context_sections.append(
            "## PR UNDER REVIEW — LATEST CODE (AUTHORITATIVE for this ticket)\n"
            "The following is the diff of the Pull Request under review. Base the "
            "test cases on THIS code. Where it conflicts with the architecture maps "
            "or packed context below (which track the main branch), trust this.\n\n"
            + pr_context
        )
    context_sections.extend(
        [
            "## RepoTree Architecture Maps\n" + arch_maps,
            semantic_context,
            "## Targeted Repomix Packed Context\n"
            + (repomix_file_context or "(no packed file snippets available)"),
        ]
    )
    arch_context = "\n\n".join(context_sections)
    system, user = _build_prompt(ticket, arch_context, style, audience)

    result = llm.complete(
        system=system, user=user,
        cache_system=False,  # system prompt varies by style; not worth caching
        max_tokens=cfg.max_output_tokens,
    )
    log.info(
        "test cases generated for %s: input=%d, output=%d, stop=%s",
        ticket.key, result.input_tokens, result.output_tokens, result.stop_reason,
    )

    return GeneratedTestCases(
        ticket_key=ticket.key,
        grounded_repos=relevant,
        style=style,
        test_cases=result.text,
        semantic_hits_count=len(semantic_hits),
        files_touched_count=len(_unique_paths(semantic_hits)),
        architecture_context_chars=len(arch_maps),
        repomix_context_chars=len(repomix_file_context),
    )
