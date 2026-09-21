"""Qdrant semantic retrieval for test-case generation.

RepoTree uses Repomix-derived architecture maps as its main grounding source.
This module adds the vector side: embed the ticket text with Ollama and fetch
the most relevant source-file snippets from the existing Qdrant collections.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .config import Config

log = logging.getLogger(__name__)


CODEBASE_EMBEDDING_MODELS: dict[str, dict[str, str]] = {
    "codebase_bge_m3": {
        "ollama_model": "bge-m3",
        "label": "BGE-M3",
    },
    "codebase_qwen3_0_6b": {
        "ollama_model": "qwen3:0.6b",
        "label": "Qwen3 0.6B",
    },
    "codebase_mxbai_large": {
        "ollama_model": "mxbai-embed-large",
        "label": "mxbai large",
    },
}


@dataclass
class SemanticHit:
    score: float
    path: str
    repo: str
    repo_name: str
    language: str
    text: str


class OllamaEmbedder:
    """Small Ollama /api/embed client for one-off query embeddings."""

    def __init__(self, base_url: str, model: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import requests

            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            names = {m.get("name", "") for m in resp.json().get("models", [])}
            base = self.model.split(":")[0]
            self._available = self.model in names or any(n.split(":")[0] == base for n in names)
            if not self._available:
                log.warning("Ollama model '%s' not found. Available: %s", self.model, sorted(names))
        except Exception as exc:
            log.warning("Ollama not reachable at %s: %s", self.base_url, exc)
            self._available = False
        return self._available

    def embed(self, text: str) -> Optional[list[float]]:
        try:
            import requests

            resp = requests.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": [text]},
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            embeddings = resp.json().get("embeddings") or []
            if not embeddings:
                log.warning("Ollama returned no embedding for query")
                return None
            return embeddings[0]
        except Exception as exc:
            log.warning("Ollama embed request failed: %s", exc)
            return None


def semantic_search_codebase(
    query: str,
    *,
    cfg: Config,
    repo_names: list[str],
    embedding_model: str,
    top_k: int,
) -> list[SemanticHit]:
    """Return Qdrant source-file hits for the ticket query.

    This is best-effort. If Ollama, Qdrant, or the requested collection is not
    available, generation continues with Repomix context only.
    """
    if top_k <= 0:
        return []
    if not cfg.qdrant_url:
        log.info("QDRANT_URL not configured; skipping semantic code search")
        return []

    model_cfg = CODEBASE_EMBEDDING_MODELS.get(
        embedding_model,
        CODEBASE_EMBEDDING_MODELS["codebase_bge_m3"],
    )
    embedder = OllamaEmbedder(
        base_url=cfg.ollama_url,
        model=model_cfg["ollama_model"],
        timeout_seconds=cfg.ollama_embed_timeout_seconds,
    )
    if not embedder.is_available():
        log.warning("Ollama model %s unavailable; skipping semantic code search", model_cfg["ollama_model"])
        return []

    query_vector = embedder.embed(query)
    if not query_vector:
        return []

    try:
        from qdrant_client import QdrantClient
    except ImportError:
        log.warning("qdrant-client not installed; skipping semantic code search")
        return []

    try:
        client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None)
        search_filter = _repo_filter(repo_names)

        try:
            response = client.query_points(
                collection_name=embedding_model,
                query=query_vector,
                limit=top_k,
                query_filter=search_filter,
                with_payload=True,
            )
            scored_points = response.points
        except AttributeError:
            scored_points = client.search(
                collection_name=embedding_model,
                query_vector=query_vector,
                limit=top_k,
                query_filter=search_filter,
                with_payload=True,
            )

        hits: list[SemanticHit] = []
        for row in scored_points:
            payload: dict[str, Any] = getattr(row, "payload", None) or {}
            hits.append(
                SemanticHit(
                    score=float(getattr(row, "score", 0.0) or 0.0),
                    path=str(payload.get("path") or ""),
                    repo=str(payload.get("repo") or ""),
                    repo_name=str(payload.get("repo_name") or ""),
                    language=str(payload.get("language") or ""),
                    text=str(payload.get("text") or "")[: cfg.semantic_hit_text_chars],
                )
            )
        log.info("Qdrant semantic code search returned %d hits from %s", len(hits), embedding_model)
        return hits
    except Exception as exc:
        log.warning("Qdrant semantic code search failed: %s", exc)
        return []


def _repo_filter(repo_names: list[str]):
    values = sorted(
        {
            value
            for repo in repo_names
            for value in (repo, repo.split("/")[-1])
            if value
        }
    )
    if not values:
        return None

    try:
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        match = MatchAny(any=values)
        return Filter(
            should=[
                FieldCondition(key="repo", match=match),
                FieldCondition(key="repo_name", match=match),
            ]
        )
    except Exception:
        pass

    if len(values) != 1:
        log.warning("qdrant-client lacks MatchAny support; skipping multi-repo filter")
        return None

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    return Filter(
        should=[
            FieldCondition(key="repo", match=MatchValue(value=values[0])),
            FieldCondition(key="repo_name", match=MatchValue(value=values[0])),
        ]
    )
