"""Unit tests for app.rca.fix_links — ticket→fix linkage (read-only signal)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.rca import fix_links
from tests.rca_helpers import init_repo, commit, make_settings


class KeyMatchingTests(unittest.TestCase):
    def test_key_regex_word_boundary(self):
        found = set(fix_links._KEY_RE.findall("Fix RFT-475 and AIGOV-66 not RFT-4750x"))
        self.assertIn("RFT-475", found)
        self.assertIn("AIGOV-66", found)

    def test_key_in_message_exact(self):
        self.assertTrue(fix_links._key_in_message("RFT-475", "fix RFT-475 now"))
        # prefix must not match a longer key
        self.assertFalse(fix_links._key_in_message("RFT-47", "fix RFT-475 now"))
        self.assertFalse(fix_links._key_in_message("RFT-475", "fix RFT-4750 now"))


class GitFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = make_settings(self.root)
        self.repo = init_repo(self.root, "svc", {"a.py": "x=1\n"})
        commit(self.repo, "RFT-900 first change")
        # second commit touching another file, unrelated key
        (self.repo / "b.py").write_text("y=2\n", encoding="utf-8")
        from tests.rca_helpers import git
        git(self.repo, "add", "-A")
        commit(self.repo, "RFT-901 unrelated")

    def tearDown(self):
        self._tmp.cleanup()

    def test_link_ticket_git_resolves_commit_and_files(self):
        links = fix_links.link_ticket_git(self.settings, "RFT-900", "svc")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].repo, "svc")
        self.assertEqual(links[0].source, "git")
        self.assertIn("a.py", links[0].changed_files)

    def test_link_ticket_git_no_match(self):
        self.assertEqual(fix_links.link_ticket_git(self.settings, "RFT-555", "svc"), [])


class FakeSession:
    """Minimal Neo4j session stub returning canned commit rows."""
    def __init__(self, commit_rows, file_rows):
        self._commit_rows = commit_rows
        self._file_rows = file_rows

    def run(self, query, **params):
        if "TOUCHES" in query:
            return [r for r in self._file_rows if r["sha"] in params.get("shas", [])]
        return list(self._commit_rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self, database=None):
        return self._session

    def close(self):
        pass


class Neo4jBackfillTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(Path("/tmp"))

    def test_backfill_neo4j_matches_and_attaches_files(self):
        commit_rows = [
            {"repo": "svc", "sha": "s1", "message": "RFT-475 fix penny drop"},
            {"repo": "svc", "sha": "s2", "message": "chore: bump deps"},  # no key
            {"repo": "web", "sha": "s3", "message": "AIGOV-66 retry logic"},
        ]
        file_rows = [
            {"sha": "s1", "files": ["src/penny.py", "src/util.py"]},
            {"sha": "s3", "files": ["app/retry.ts"]},
        ]
        driver = FakeDriver(FakeSession(commit_rows, file_rows))

        orig = fix_links._neo4j_driver
        fix_links._neo4j_driver = lambda s: driver
        try:
            links = fix_links.backfill_neo4j(self.settings, {"RFT-475", "AIGOV-66"})
        finally:
            fix_links._neo4j_driver = orig

        by_key = {l.ticket_key: l for l in links}
        self.assertEqual(set(by_key), {"RFT-475", "AIGOV-66"})
        self.assertIn("src/penny.py", by_key["RFT-475"].changed_files)
        self.assertEqual(by_key["AIGOV-66"].repo, "web")
        self.assertTrue(all(l.source == "neo4j" for l in links))


if __name__ == "__main__":
    unittest.main()
