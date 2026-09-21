"""Helpers for parsing the three-section markdown format the LLM returns.

The contract (set in prompts.py): output always contains exactly three
top-level sections in this order:
    ## ARCHITECTURE
    ## ROUTES
    ## DATA_MODELS

These helpers split and re-assemble that format. Robust to extra whitespace
but strict about the headers themselves — a malformed LLM response should
raise so we can detect it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SECTIONS = ("ARCHITECTURE", "ROUTES", "DATA_MODELS")
_HEADER_RE = re.compile(r"^##\s+(ARCHITECTURE|ROUTES|DATA_MODELS)\s*$", re.MULTILINE)


@dataclass
class ParsedSections:
    architecture: str
    routes: str
    data_models: str

    def as_dict(self) -> dict[str, str]:
        return {
            "architecture": self.architecture,
            "routes": self.routes,
            "data_models": self.data_models,
        }

    def render(self) -> str:
        """Re-emit as a single markdown string in canonical format."""
        return (
            f"## ARCHITECTURE\n{self.architecture.strip()}\n\n"
            f"## ROUTES\n{self.routes.strip()}\n\n"
            f"## DATA_MODELS\n{self.data_models.strip()}\n"
        )


class MalformedLLMOutput(ValueError):
    pass


def parse_sections(text: str) -> ParsedSections:
    """Split LLM output into the three sections.

    Raises MalformedLLMOutput if any section is missing or out of order.
    """
    matches = list(_HEADER_RE.finditer(text))
    if len(matches) < 3:
        raise MalformedLLMOutput(
            f"Expected 3 section headers, found {len(matches)}. "
            f"Headers seen: {[m.group(1) for m in matches]}"
        )

    headers_found = [m.group(1) for m in matches[:3]]
    if tuple(headers_found) != SECTIONS:
        raise MalformedLLMOutput(
            f"Section headers wrong or out of order. Expected {SECTIONS}, got {tuple(headers_found)}"
        )

    spans = []
    for i, m in enumerate(matches[:3]):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches[:3]) else len(text)
        spans.append(text[body_start:body_end].strip())

    return ParsedSections(
        architecture=spans[0],
        routes=spans[1],
        data_models=spans[2],
    )
