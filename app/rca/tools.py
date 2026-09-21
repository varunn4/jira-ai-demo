"""Phase F — read-only investigation tools for the agentic loop.

Seven tools, every one read-only against our real backends. None may write
files, mutate a repo, run code, or call any state-changing API:

  grep_codebase(repo, pattern, is_regex)   git grep over the working tree
  read_file(repo, path, start, end)        bounded file slice
  find_references(repo, symbol)             Neo4j CALLS graph
  git_blame(repo, path, start, end)         git blame
  git_log(repo, path, limit)                git history
  search_similar_tickets(query, k)          Qdrant ticket vectors
  semantic_code_search(query, repo, k)      Qdrant code_chunks

Each returns a JSON-serializable dict; failures degrade to {"error": ...} so the
agent can recover rather than crash the run.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import Settings
from app.rca import code_index, repos as repo_access

log = logging.getLogger(__name__)


# Anthropic tool schemas (read-only investigation surface).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "grep_codebase",
        "description": "Search a repo's tracked files for a literal string or regex. "
                       "Returns matching path:line:text. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo name to search."},
                "pattern": {"type": "string", "description": "Text or regex to find."},
                "is_regex": {"type": "boolean", "description": "Treat pattern as regex.",
                             "default": False},
            },
            "required": ["repo", "pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a slice of a file (1-based inclusive line range). Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string", "description": "Repo-relative file path."},
                "start": {"type": "integer", "description": "First line (optional)."},
                "end": {"type": "integer", "description": "Last line (optional)."},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "find_references",
        "description": "Find functions that CALL the given symbol, and where it is "
                       "defined, via the code graph. Use to trace call paths. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "symbol": {"type": "string", "description": "Function/method name."},
            },
            "required": ["repo", "symbol"],
        },
    },
    {
        "name": "git_blame",
        "description": "Blame a line range: who last changed each line and when. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": ["repo", "path", "start", "end"],
        },
    },
    {
        "name": "git_log",
        "description": "Recent commits for a repo or a specific file. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string", "description": "Optional file path."},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "search_similar_tickets",
        "description": "Find historically similar Jira tickets (semantic). Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "semantic_code_search",
        "description": "Semantic search over function/class code chunks. Omit `repo` to "
                       "search ALL indexed repos — do this to discover where relevant "
                       "code lives when the seeded repo yields nothing. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "repo": {"type": "string", "description": "Optional repo to scope to."},
                "k": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_repos",
        "description": "List the repositories available to investigate and which are in "
                       "the current focus scope. Use this to find the right repo when the "
                       "seeded repos come up empty. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class ToolContext:
    """Holds settings + the repos the run is scoped to (enforced read-only)."""

    def __init__(self, settings: Settings, allowed_repos: Optional[list[str]] = None):
        self.settings = settings
        self.allowed_repos = set(allowed_repos or [])
        self._similar_finder = None

    def _check_repo(self, repo: str) -> Optional[dict[str, Any]]:
        if self.allowed_repos and repo not in self.allowed_repos:
            return {"error": f"repo '{repo}' is out of scope for this run "
                             f"(in scope: {sorted(self.allowed_repos)}). Run an unscoped "
                             f"semantic_code_search or call list_repos to find where the "
                             f"relevant code lives — repos a semantic search surfaces "
                             f"become reachable automatically."}
        return None

    def _expand_scope(self, repos: Any) -> None:
        """Widen the focus scope to include repos surfaced by the evidence.

        The run seeds `allowed_repos` with the localizer's candidates, but the
        real code sometimes lives in a repo the localizer missed (e.g. a legacy
        app). When a semantic search surfaces a repo, make it reachable for
        follow-up grep/read so the agent can chase the lead instead of dead-ending
        on an out-of-scope error. Every tool is read-only, so widening the scope
        to already-indexed repos is safe. No-op when the run is unrestricted
        (empty `allowed_repos`).
        """
        if not self.allowed_repos:
            return
        for repo in repos:
            if repo and repo not in self.allowed_repos:
                self.allowed_repos.add(repo)
                log.info("RCA scope expanded to include repo '%s' "
                         "(surfaced by semantic_code_search)", repo)

    # ── dispatch ──────────────────────────────────────────────────────────────

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            handler = getattr(self, f"_t_{name}", None)
            if handler is None:
                return {"error": f"unknown tool: {name}"}
            return handler(args)
        except repo_access.RepoAccessError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # never let a tool crash the loop
            log.warning("RCA tool %s failed: %s", name, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _t_grep_codebase(self, a: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_repo(a.get("repo", ""))):
            return err
        hits = repo_access.grep(self.settings, a["repo"], a["pattern"],
                                bool(a.get("is_regex", False)))
        out: dict[str, Any] = {"matches": hits, "count": len(hits)}
        # A capped result means the pattern is too broad to be evidence — steer the
        # agent back to semantic discovery instead of letting it drown in matches.
        if len(hits) >= repo_access._MAX_GREP_MATCHES:
            out["hint"] = (
                f"'{a['pattern']}' matched too many lines (capped at "
                f"{repo_access._MAX_GREP_MATCHES}) — it is too broad to be evidence. "
                "grep is for a specific identifier you already have; to find the "
                "relevant code from a concept, use semantic_code_search instead."
            )
        return out

    def _t_read_file(self, a: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_repo(a.get("repo", ""))):
            return err
        return repo_access.read_file(self.settings, a["repo"], a["path"],
                                     a.get("start"), a.get("end"))

    def _t_find_references(self, a: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_repo(a.get("repo", ""))):
            return err
        return find_references(self.settings, a["repo"], a["symbol"])

    def _t_git_blame(self, a: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_repo(a.get("repo", ""))):
            return err
        rows = repo_access.git_blame(self.settings, a["repo"], a["path"],
                                     int(a["start"]), int(a["end"]))
        return {"blame": rows}

    def _t_git_log(self, a: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_repo(a.get("repo", ""))):
            return err
        rows = repo_access.git_log(self.settings, a["repo"], a.get("path"),
                                   int(a.get("limit", 10)))
        return {"commits": rows}

    def _t_search_similar_tickets(self, a: dict[str, Any]) -> dict[str, Any]:
        if self._similar_finder is None:
            from app.similar_ticket_finder import SimilarTicketFinder
            self._similar_finder = SimilarTicketFinder(self.settings)
        res = self._similar_finder.find_similar(a["query"][:200], a["query"])
        return {"tickets": res.get("tickets", []), "method": res.get("search_method")}

    def _t_semantic_code_search(self, a: dict[str, Any]) -> dict[str, Any]:
        repo = a.get("repo")
        if repo and (err := self._check_repo(repo)):
            return err
        hits = code_index.search(self.settings, a["query"], repo=repo,
                                 k=int(a.get("k", 8)))
        # Follow the evidence: any repo the index surfaces becomes reachable for
        # follow-up grep/read, even if the localizer didn't seed it.
        self._expand_scope(h.get("repo", "") for h in hits)
        return {"results": hits}

    def _t_list_repos(self, a: dict[str, Any]) -> dict[str, Any]:
        available = repo_access.list_repos(self.settings)
        return {
            "repos": available,
            "in_scope": sorted(self.allowed_repos) if self.allowed_repos else available,
        }


def find_references(settings: Settings, repo: str, symbol: str) -> dict[str, Any]:
    """Callers of `symbol` + its definition site(s), from the Neo4j graph."""
    if not settings.neo4j_password:
        return {"error": "neo4j not configured", "callers": [], "definitions": []}
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(settings.neo4j_uri,
                                  auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        with driver.session(database=settings.neo4j_database) as s:
            callers = [dict(r) for r in s.run(
                """
                MATCH (caller:Function)-[:CALLS]->(fn:Function {name: $symbol})
                WHERE fn.repo = $repo
                RETURN DISTINCT caller.name AS caller, caller.file AS file,
                       caller.start_line AS line LIMIT 50
                """, symbol=symbol, repo=repo)]
            defs = [dict(r) for r in s.run(
                """
                MATCH (fn:Function {name: $symbol, repo: $repo})
                RETURN fn.file AS file, fn.start_line AS start_line,
                       fn.end_line AS end_line LIMIT 25
                """, symbol=symbol, repo=repo)]
        return {"symbol": symbol, "repo": repo, "definitions": defs, "callers": callers}
    finally:
        driver.close()
