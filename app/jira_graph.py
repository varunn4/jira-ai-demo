"""Jira ticket text helpers.

Atlassian Document Format (ADF) parsing shared by the Jira fetcher and the
Qdrant embedding pipeline.
"""
from __future__ import annotations

from typing import Any


def _adf_to_text(node: Any, _depth: int = 0) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(child, _depth + 1) for child in node.get("content", [])]
        sep = "\n" if node.get("type") in ("paragraph", "heading", "listItem") else " "
        return sep.join(p for p in parts if p)
    return ""
