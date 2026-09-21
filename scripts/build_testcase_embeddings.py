#!/usr/bin/env python3
"""Backfill / rebuild the `test_cases` Qdrant collection.

The regression flag in n8n Workflow 1 (/analyze-ticket/regression) matches a new
incoming ticket against embeddings of previously generated test cases. New test
cases are embedded automatically on write, but existing rows need a one-time
backfill — that is what this script does.

Run from the repository root (so `app` is importable):

    python scripts/build_testcase_embeddings.py                 # all test cases
    python scripts/build_testcase_embeddings.py RFT-123         # one ticket
    python scripts/build_testcase_embeddings.py RFT-123 dev     # one ticket, dev phase

Inside Docker:

    docker compose exec jira-ai-api python scripts/build_testcase_embeddings.py

Requires Ollama (OLLAMA_URL / OLLAMA_EMBED_MODEL) and Qdrant (QDRANT_URL) to be
reachable, plus DATABASE_URL.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.testcase_embeddings import build_testcase_embeddings  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    jira_ticket_id = args[0] if len(args) > 0 else None
    phase = args[1] if len(args) > 1 else None

    def _progress(done: int, total: int) -> None:
        if total:
            print(f"  embedding {done}/{total}", end="\r", flush=True)

    print(
        f"Building test-case embeddings "
        f"(ticket={jira_ticket_id or 'ALL'}, phase={phase or 'ALL'})…"
    )
    result = build_testcase_embeddings(
        settings, jira_ticket_id, phase, progress_callback=_progress
    )
    print()
    print(
        f"Done: rows={result['rows']} embedded={result['embedded']} "
        f"stored={result['stored']} method={result['method']}"
    )
    if result["method"] in {"unavailable", "none"}:
        print(
            "WARNING: embeddings were not stored. Check that Ollama and Qdrant "
            "are reachable and DATABASE_URL is set."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
