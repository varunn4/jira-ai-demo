"""Thin batched-write layer over the Neo4j driver.

Nodes are MERGEd on a single key property; relationships MATCH both endpoints
(written earlier) and MERGE the edge. Everything is chunked with UNWIND so a
repo with thousands of commits/files writes in a handful of round-trips.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from neo4j import GraphDatabase

log = logging.getLogger(__name__)

# (label, key property) — uid unless the node has a natural global key.
_NODE_KEYS = {
    "Repo": "name",
    "Author": "email",
    "Module": "name",
    "ExternalClass": "name",
    "Branch": "uid",
    "Commit": "uid",
    "Directory": "uid",
    "File": "uid",
    "Class": "uid",
    "Interface": "uid",
    "Function": "uid",
    "Parameter": "uid",
}


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str, database: str,
                 batch_size: int = 1000, dry_run: bool = False):
        self.database = database
        self.batch_size = batch_size
        self.dry_run = dry_run
        self.counts: dict[str, int] = {}
        self._driver = None
        if not dry_run:
            self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def verify(self) -> None:
        if self.dry_run:
            return
        self._driver.verify_connectivity()

    def close(self) -> None:
        if self._driver:
            self._driver.close()

    def __enter__(self) -> "Neo4jWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _run(self, query: str, **params) -> None:
        if self.dry_run:
            return
        with self._driver.session(database=self.database) as session:
            session.run(query, **params).consume()

    def ensure_schema(self) -> None:
        for label, key in _NODE_KEYS.items():
            self._run(
                f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{label}`) "
                f"REQUIRE n.`{key}` IS UNIQUE"
            )
        log.info("Schema constraints ensured")

    def wipe(self, labels: Iterable[str]) -> None:
        """DETACH DELETE every node carrying one of `labels`, in batches."""
        for label in labels:
            self._run(
                f"MATCH (n:`{label}`) "
                f"CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF 5000 ROWS"
            )
        log.info("Wiped labels: %s", ", ".join(labels))

    def wipe_all(self) -> None:
        self._run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS")
        log.info("Wiped ENTIRE database")

    def write_nodes(self, label: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        key = _NODE_KEYS[label]
        query = (
            f"UNWIND $rows AS row "
            f"MERGE (n:`{label}` {{`{key}`: row.`{key}`}}) "
            f"SET n += row"
        )
        for chunk in _chunks(rows, self.batch_size):
            self._run(query, rows=chunk)
        self.counts[label] = self.counts.get(label, 0) + len(rows)

    def write_rels(
        self, rel_type: str, start_label: str, end_label: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """rows: list of {"start": <start key val>, "end": <end key val>, ...props}."""
        if not rows:
            return
        sk = _NODE_KEYS[start_label]
        ek = _NODE_KEYS[end_label]
        query = (
            f"UNWIND $rows AS row "
            f"MATCH (a:`{start_label}` {{`{sk}`: row.start}}) "
            f"MATCH (b:`{end_label}` {{`{ek}`: row.end}}) "
            f"MERGE (a)-[r:`{rel_type}`]->(b) "
            f"SET r += row.props"
        )
        payload = [
            {"start": r["start"], "end": r["end"],
             "props": {k: v for k, v in r.items() if k not in ("start", "end")}}
            for r in rows
        ]
        for chunk in _chunks(payload, self.batch_size):
            self._run(query, rows=chunk)
        self.counts[f"[:{rel_type}]"] = self.counts.get(f"[:{rel_type}]", 0) + len(rows)


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
