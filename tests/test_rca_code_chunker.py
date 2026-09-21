"""Unit tests for app.rca.code_chunker — tree-sitter function/class chunks."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.rca import code_chunker
from tests.rca_helpers import init_repo, commit, make_settings

_PY = '''\
import os


class PaymentService:
    """Handles payments."""

    def charge(self, amount):
        return self._call(amount)

    def _call(self, amount):
        return amount * 2


def standalone(x):
    return x + 1
'''


class ChunkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = make_settings(self.root)
        self.repo = init_repo(self.root, "svc", {"pkg/payment.py": _PY, "readme.md": "hi"})
        commit(self.repo, "init")

    def tearDown(self):
        self._tmp.cleanup()

    def test_language_for(self):
        self.assertEqual(code_chunker.language_for("a/b.py"), "python")
        self.assertEqual(code_chunker.language_for("a/b.ts"), "typescript")
        self.assertIsNone(code_chunker.language_for("a/b.md"))

    def test_chunk_file_symbols(self):
        chunks = code_chunker.chunk_file(self.settings, "svc", "pkg/payment.py")
        by_symbol = {c.symbol: c for c in chunks}
        self.assertIn("PaymentService", by_symbol)
        self.assertIn("charge", by_symbol)
        self.assertIn("_call", by_symbol)
        self.assertIn("standalone", by_symbol)

        cls = by_symbol["PaymentService"]
        self.assertEqual(cls.chunk_type, "class")
        self.assertEqual(cls.start_line, 4)

        self.assertEqual(by_symbol["charge"].chunk_type, "method")
        self.assertEqual(by_symbol["standalone"].chunk_type, "function")
        # body text is captured
        self.assertIn("amount * 2", by_symbol["_call"].text)

    def test_chunk_skips_non_source(self):
        self.assertEqual(code_chunker.chunk_file(self.settings, "svc", "readme.md"), [])

    def test_iter_repo_chunks_only_source(self):
        chunks = list(code_chunker.iter_repo_chunks(self.settings, "svc"))
        self.assertTrue(all(c.file_path.endswith(".py") for c in chunks))
        self.assertGreaterEqual(len(chunks), 4)

    def test_point_seed_stable(self):
        chunks = code_chunker.chunk_file(self.settings, "svc", "pkg/payment.py")
        seeds = [c.point_seed() for c in chunks]
        self.assertEqual(len(seeds), len(set(seeds)))  # unique per symbol


if __name__ == "__main__":
    unittest.main()
