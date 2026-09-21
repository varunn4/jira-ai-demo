"""Unit tests for app.rca.code_index — delta-sync orchestration (no network).

The Qdrant client, Ollama embedder, and Postgres state are faked so the
skip/full/delta/delete decision logic is exercised without external services.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from app.rca import code_index
from app.rca.code_chunker import CodeChunk
from tests.rca_helpers import make_settings


class FakeClient:
    def __init__(self):
        self.deleted: list = []
        self.upserts: list = []


def _chunk(symbol, line):
    return CodeChunk("svc", "a.py", symbol, line, line + 5, "python", "function", "body")


class IndexRepoTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(Path("/tmp"))
        self._orig = {name: getattr(code_index, name) for name in (
            "init_schema", "get_indexed_sha", "_set_indexed_sha", "_qdrant_client",
            "_ensure_collection", "_upsert_chunks", "_delete_file_points",
            "_changed_files", "iter_repo_chunks",
        )}
        self._orig_head = code_index.repos.head_sha

        self.state = {}
        code_index.init_schema = lambda s: None
        code_index.get_indexed_sha = lambda s, repo: self.state.get(repo)
        code_index._set_indexed_sha = lambda s, repo, sha, n: self.state.__setitem__(repo, sha)
        code_index._qdrant_client = lambda s: FakeClient()
        code_index._ensure_collection = lambda c, n: None
        self.deleted_files = []
        code_index._delete_file_points = lambda c, n, repo, files: self.deleted_files.extend(files)
        code_index._upsert_chunks = lambda s, c, n, chunks, sha: len(chunks)

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(code_index, name, fn)
        code_index.repos.head_sha = self._orig_head

    def test_skip_when_sha_unchanged(self):
        self.state["svc"] = "HEAD1"
        code_index.repos.head_sha = lambda s, repo: "HEAD1"
        code_index.iter_repo_chunks = lambda s, repo, files: iter([])
        res = code_index.index_repo(self.settings, "svc")
        self.assertTrue(res.skipped)
        self.assertEqual(res.chunks_written, 0)

    def test_full_index_when_no_prev(self):
        code_index.repos.head_sha = lambda s, repo: "HEAD1"
        seen = {}
        def _iter(s, repo, files):
            seen["files"] = files  # None => all tracked
            return iter([_chunk("f1", 1), _chunk("f2", 10)])
        code_index.iter_repo_chunks = _iter
        res = code_index.index_repo(self.settings, "svc")
        self.assertFalse(res.skipped)
        self.assertIsNone(seen["files"])          # full scan
        self.assertEqual(res.chunks_written, 2)
        self.assertEqual(self.state["svc"], "HEAD1")

    def test_delta_reindexes_only_changed_and_deletes_stale(self):
        self.state["svc"] = "OLD"
        code_index.repos.head_sha = lambda s, repo: "NEW"
        code_index._changed_files = lambda s, repo, old: (["a.py"], ["gone.py"])
        captured = {}
        def _iter(s, repo, files):
            captured["files"] = files
            return iter([_chunk("f1", 1)])
        code_index.iter_repo_chunks = _iter
        res = code_index.index_repo(self.settings, "svc")
        self.assertEqual(captured["files"], ["a.py"])        # only changed file
        self.assertIn("a.py", self.deleted_files)            # stale points dropped
        self.assertIn("gone.py", self.deleted_files)         # deleted file dropped
        self.assertEqual(res.files_deleted, 1)
        self.assertEqual(self.state["svc"], "NEW")

    def test_force_full_ignores_prev(self):
        self.state["svc"] = "NEW"
        code_index.repos.head_sha = lambda s, repo: "NEW"
        code_index.iter_repo_chunks = lambda s, repo, files: iter([_chunk("f1", 1)])
        res = code_index.index_repo(self.settings, "svc", force_full=True)
        self.assertFalse(res.skipped)
        self.assertEqual(res.chunks_written, 1)


if __name__ == "__main__":
    unittest.main()
