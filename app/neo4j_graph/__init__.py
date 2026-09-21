"""Codebase -> Neo4j knowledge-graph builder + analytics for the admin UI.

Builds the git layer (Repo/Commit/Author/Branch/File/Directory) and a
language-agnostic code layer (Class/Function/Interface/Module/Parameter +
CALLS/IMPORTS/INHERITS via tree-sitter) for the active (non-stale) repositories
discovered by app.repository_discovery.
"""
