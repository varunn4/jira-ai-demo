"""Unit tests for the RCA engine (Phases D–I): retrieval RRF, synthesis gate,
agent loop, tools scope, store lifecycle, document fix-mode."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.rca import agent, retrieval, store, synthesis, tools
from app.rca.retrieval import Candidate
from tests.rca_helpers import init_repo, commit, make_settings


# ── Phase E: RRF fusion ───────────────────────────────────────────────────────

class RRFTests(unittest.TestCase):
    def test_fuse_accumulates_and_tags(self):
        fused = {}
        retrieval._fuse(fused, [Candidate("r", "a.py"), Candidate("r", "b.py")], "grep")
        retrieval._fuse(fused, [Candidate("r", "a.py")], "semantic")
        a = fused[("r", "a.py")]
        # a.py surfaced by two retrievers → higher score, both tags
        self.assertEqual(a.retrievers, {"grep", "semantic"})
        self.assertGreater(a.score, fused[("r", "b.py")].score)

    def test_fuse_enriches_line_info(self):
        fused = {}
        retrieval._fuse(fused, [Candidate("r", "a.py")], "grep")
        retrieval._fuse(fused, [Candidate("r", "a.py", symbol="foo", start_line=10)], "stack")
        self.assertEqual(fused[("r", "a.py")].symbol, "foo")
        self.assertEqual(fused[("r", "a.py")].start_line, 10)


# ── Phase G: synthesis normalization + fix gate ───────────────────────────────

class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
    def complete(self, system, user, *, max_tokens=4096):
        return self.payload


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(Path("/tmp"))

    def _diag(self, confidence, with_fix, *, evidence_count=2, insufficient=False,
             confirmed=True):
        import json
        d = {
            "insufficient_data": insufficient,
            "issue_classification": "Runtime Bug",
            "confidence": confidence,  # High | Medium | Low
            "facts": ["f1", "f2"],
            "inferences": ["i1"],
            "unknowns": ["u1"],
            "root_cause": "Null deref in a.py because f() returns None on empty input.",
            "root_cause_confirmed": confirmed,
            "root_cause_location": {"repo": "svc", "file": "a.py", "symbol": "f", "lines": "1-9"},
            "evidence": [{"category": "Code", "detail": "d", "ref": f"a.py:{i}"}
                         for i in range(evidence_count)],
            "contributing_factors": [],
            "recommended_fix": "Guard against a None return from f()." if with_fix else None,
        }
        return json.dumps(d)

    def test_confirmed_gives_high_and_fix(self):
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(self._diag("High", with_fix=True, confirmed=True)))
        self.assertIsNotNone(out["recommended_fix"])
        self.assertEqual(out["confidence_label"], "High")
        self.assertEqual(out["confidence"], 0.95)  # internal numeric for the gate
        self.assertEqual(out["root_cause_status"], "confirmed")

    def test_unconfirmed_gives_medium_no_fix(self):
        # RULE 5/6: a leading-but-unverified cause is Medium and carries no fix.
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(self._diag("High", with_fix=True, confirmed=False)))
        self.assertEqual(out["root_cause_status"], "most_likely")
        self.assertEqual(out["confidence_label"], "Medium")
        self.assertIsNone(out["recommended_fix"])
        self.assertNotEqual(out["root_cause"], "Undetermined.")  # candidate retained

    def test_confidence_is_derived_not_taken_from_model(self):
        # RULE 6: confirmed ⇒ High even if the model self-reports "Low".
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(self._diag("Low", with_fix=True, confirmed=True)))
        self.assertEqual(out["confidence_label"], "High")
        self.assertEqual(out["root_cause_status"], "confirmed")

    def test_single_conclusive_evidence_can_confirm(self):
        # RULE 5(b): one conclusive source is enough — no hard ≥2 gate anymore.
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(
                self._diag("High", with_fix=True, confirmed=True, evidence_count=1)))
        self.assertEqual(out["root_cause_status"], "confirmed")
        self.assertEqual(out["confidence_label"], "High")
        self.assertIsNotNone(out["recommended_fix"])

    def test_evidence_is_categorized(self):
        import json
        raw = json.dumps({
            "confidence": "High", "root_cause_confirmed": True,
            "root_cause": "cause", "evidence": [
                {"category": "Logs", "detail": "timeout", "ref": "app.log:12"},
                {"type": "git_history", "detail": "commit abc", "ref": "abc123"},
            ]})
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(raw))
        cats = [e["category"] for e in out["evidence"]]
        self.assertEqual(cats, ["Logs", "Git"])  # explicit + mapped-from-legacy-type

    def test_introduced_by_requires_a_commit(self):
        import json
        no_commit = json.dumps({
            "confidence": "High", "root_cause_confirmed": True, "root_cause": "c",
            "introduced_by": {"name": "Someone", "commit": None, "date": None},
            "evidence": [{"category": "Code", "detail": "d", "ref": "a.py:1"},
                         {"category": "Code", "detail": "e", "ref": "a.py:2"}]})
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(no_commit))
        self.assertIsNone(out["introduced_by"])  # a bare name is not authorship

    def test_zero_evidence_forces_undetermined(self):
        # A stated cause with no evidence at all collapses to Undetermined.
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(self._diag("High", with_fix=True, evidence_count=0)))
        self.assertEqual(out["root_cause"], "Undetermined.")
        self.assertEqual(out["confidence_label"], "Low")
        self.assertIsNone(out["recommended_fix"])

    def test_insufficient_data_short_circuits(self):
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(self._diag("High", with_fix=True, insufficient=True)))
        self.assertEqual(out["root_cause"], synthesis.INSUFFICIENT_DATA_MESSAGE)
        self.assertEqual(out["confidence_label"], "Low")
        self.assertEqual(out["issue_classification"], "Cannot Determine")  # RULE 2
        self.assertIsNone(out["recommended_fix"])

    def test_classification_defaults_to_cannot_determine(self):
        import json
        bad = json.dumps({"issue_classification": "banana", "confidence": "High",
                          "root_cause": "x", "evidence": [{"type": "a", "detail": "b"}]})
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM(bad))
        self.assertEqual(out["issue_classification"], "Cannot Determine")

    def test_user_message_handles_large_observations(self):
        # Regression: a large tool observation must not break JSON building.
        big = {"matches": [{"text": "x" * 50} for _ in range(200)]}
        trace = [{"tool": "grep_codebase", "input": {"repo": "svc"}, "observation": big}]
        msg = synthesis._user_message({}, {}, [], "found it", trace)
        self.assertIn("found it", msg)
        self.assertIn("truncated", msg)  # large observation safely truncated

    def test_truncate_observation_small_passthrough(self):
        obs = {"count": 1}
        self.assertEqual(synthesis._truncate_observation(obs, 1500), obs)

    def test_bad_json_normalizes_to_undetermined(self):
        out = synthesis.synthesize(
            self.settings, ticket={}, extracted={}, candidates=[], investigation="",
            trace=[], llm_client=FakeLLM("not json"))
        self.assertEqual(out["confidence_label"], "Low")
        self.assertEqual(out["root_cause"], "Undetermined.")
        self.assertEqual(out["issue_classification"], "Cannot Determine")


# ── Phase F: agent loop with fakes ────────────────────────────────────────────

class FakeBlock(SimpleNamespace):
    pass


class FakeResp(SimpleNamespace):
    pass


class ScriptedClient:
    """Returns a tool_use turn, then a final text turn."""
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        if self.calls == 1:
            tu = FakeBlock(type="tool_use", id="t1", name="read_file",
                           input={"repo": "svc", "path": "a.py"})
            return FakeResp(content=[tu], stop_reason="tool_use", usage=usage)
        txt = FakeBlock(type="text", text="Root cause is in a.py line 1.")
        return FakeResp(content=[txt], stop_reason="end_turn", usage=usage)


class FakeToolCtx:
    def __init__(self):
        self.dispatched = []
    def dispatch(self, name, args):
        self.dispatched.append((name, args))
        return {"content": "line 1: bug"}


class CapturingClient(ScriptedClient):
    """Records the kwargs of each create() call for prompt-cache assertions."""
    def __init__(self):
        super().__init__()
        self.seen_kwargs = []

    def create(self, **kwargs):
        self.seen_kwargs.append(kwargs)
        return super().create(**kwargs)


class AgentLoopTests(unittest.TestCase):
    def test_loop_runs_tool_then_summarizes(self):
        settings = make_settings(Path("/tmp"))
        events = []
        res = agent.run_investigation(
            settings, ticket_summary="bug", extracted={}, candidates=[],
            allowed_repos=["svc"], on_event=events.append,
            client=ScriptedClient(), tool_context=FakeToolCtx())
        self.assertIn("a.py", res.summary)
        self.assertEqual(res.stop_reason, "end_turn")
        self.assertEqual(len(res.agent_trace), 1)
        self.assertEqual(res.agent_trace[0]["tool"], "read_file")
        self.assertEqual(len(events), 1)
        self.assertEqual(res.input_tokens, 20)  # two calls × 10

    def test_prompt_caching_and_full_ticket_context(self):
        settings = make_settings(Path("/tmp"))
        client = CapturingClient()
        agent.run_investigation(
            settings, ticket_summary="bug",
            ticket_description="Notional interest miscomputed for EMI product",
            ticket_comments=["Repro: create EMI lead, observe wrong repayAmount"],
            extracted={}, candidates=[], allowed_repos=["svc"],
            client=client, tool_context=FakeToolCtx())

        # every call caches the static system prefix …
        first = client.seen_kwargs[0]
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral"})
        # … and marks exactly one rolling breakpoint on the latest turn.
        for kw in client.seen_kwargs:
            marked = [b for m in kw["messages"] if isinstance(m["content"], list)
                      for b in m["content"]
                      if isinstance(b, dict) and "cache_control" in b]
            self.assertEqual(len(marked), 1)

        # the full ticket description + comments reach the agent's seed message.
        seed_text = first["messages"][0]["content"][0]["text"]
        self.assertIn("Notional interest miscomputed", seed_text)
        self.assertIn("wrong repayAmount", seed_text)


# ── Phase F: tool repo-scope enforcement ──────────────────────────────────────

class ToolScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = make_settings(self.root)
        init_repo(self.root, "svc", {"a.py": "SECRET=1\n"})
        commit(self.root / "svc", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def test_out_of_scope_repo_blocked(self):
        ctx = tools.ToolContext(self.settings, allowed_repos=["svc"])
        out = ctx.dispatch("grep_codebase", {"repo": "other", "pattern": "x"})
        self.assertIn("error", out)
        self.assertIn("out of scope", out["error"])

    def test_in_scope_grep_works(self):
        ctx = tools.ToolContext(self.settings, allowed_repos=["svc"])
        out = ctx.dispatch("grep_codebase", {"repo": "svc", "pattern": "SECRET"})
        self.assertEqual(out["count"], 1)

    def test_unknown_tool(self):
        ctx = tools.ToolContext(self.settings, allowed_repos=["svc"])
        self.assertIn("error", ctx.dispatch("rm_rf", {}))

    def test_grep_hint_when_pattern_too_broad(self):
        # A pattern that hits the match cap is too broad to be evidence — the tool
        # should say so and point back at semantic search.
        from app.rca import repos as repo_access
        n = repo_access._MAX_GREP_MATCHES + 5
        init_repo(self.root, "broad", {
            "app/x.php": "".join(f"line aml {i}\n" for i in range(n)),
        })
        commit(self.root / "broad", "init")
        ctx = tools.ToolContext(self.settings, allowed_repos=["broad"])
        out = ctx.dispatch("grep_codebase", {"repo": "broad", "pattern": "aml"})
        self.assertEqual(out["count"], repo_access._MAX_GREP_MATCHES)
        self.assertIn("hint", out)
        self.assertIn("semantic_code_search", out["hint"])

    def test_list_repos_reports_available_and_scope(self):
        ctx = tools.ToolContext(self.settings, allowed_repos=["svc"])
        out = ctx.dispatch("list_repos", {})
        self.assertIn("svc", out["repos"])
        self.assertEqual(out["in_scope"], ["svc"])

    def test_semantic_search_expands_scope_to_surfaced_repo(self):
        # A second repo the localizer never seeded, holding the real code.
        init_repo(self.root, "legacy", {"b.py": "TARGET=1\n"})
        commit(self.root / "legacy", "init")

        ctx = tools.ToolContext(self.settings, allowed_repos=["svc"])
        # grep on the out-of-scope repo is blocked before any lead surfaces it.
        blocked = ctx.dispatch("grep_codebase", {"repo": "legacy", "pattern": "TARGET"})
        self.assertIn("error", blocked)

        # A semantic search surfaces the code living in 'legacy'…
        original = tools.code_index.search
        tools.code_index.search = lambda *a, **k: [{"repo": "legacy", "file_path": "b.py"}]
        try:
            hits = ctx.dispatch("semantic_code_search", {"query": "target"})
        finally:
            tools.code_index.search = original
        self.assertEqual(hits["results"][0]["repo"], "legacy")

        # …so the agent can now grep it to chase the lead.
        self.assertIn("legacy", ctx.allowed_repos)
        followup = ctx.dispatch("grep_codebase", {"repo": "legacy", "pattern": "TARGET"})
        self.assertEqual(followup["count"], 1)


# ── Phase H: store lifecycle (in-memory, no DB) ───────────────────────────────

class StoreTests(unittest.TestCase):
    def setUp(self):
        # no DATABASE_URL → store uses in-memory mirror only
        import dataclasses
        self.settings = dataclasses.replace(
            make_settings(Path("/tmp")),
            database_url_override="", db_host="", db_name="", db_user="",
        )

    def test_create_and_transition(self):
        st = store.RCARunStore(self.settings)
        run = st.create("OPS-1")
        self.assertEqual(run.status, store.STATUS_QUEUED)
        st.touch(run, status=store.STATUS_INVESTIGATING)
        self.assertEqual(st.get(run.run_id).status, store.STATUS_INVESTIGATING)
        st.add_trace_event(run, {"tool": "read_file"})
        self.assertEqual(len(st.get(run.run_id).agent_trace), 1)

    def test_list_recent_in_memory(self):
        st = store.RCARunStore(self.settings)
        st.create("OPS-1")
        st.create("OPS-2")
        recent = st.list_recent()
        self.assertEqual(len(recent), 2)


if __name__ == "__main__":
    unittest.main()
