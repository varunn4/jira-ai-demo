"""Ollama embedding client.

Uses Ollama's /api/embed endpoint (batch-capable) to generate dense vector
embeddings for whatever model is configured via OLLAMA_EMBED_MODEL.

Usage:
    embedder = OllamaEmbedder(base_url, model)
    if embedder.is_available():
        vectors = embedder.embed_batch(["text one", "text two"])
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

log = logging.getLogger(__name__)

_EMBED_ENDPOINT = "/api/embed"       # batch-capable (Ollama ≥ 0.1.x)
_TAGS_ENDPOINT  = "/api/tags"

# How many texts to send per HTTP request. Keeps memory / timeout manageable
# for large ticket/commit lists.
_BATCH_CHUNK = 32
_EMBED_TIMEOUT_SECONDS = 120

# When a request fails (usually a read timeout on a slow/cold model), retry the
# single-text case a few times with linear backoff before giving up. Multi-text
# chunks are split in half first — a smaller request is far more likely to beat
# the timeout — so one slow batch can't silently drop all of its texts.
_MAX_SINGLE_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 2.0


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:0.6b",
        timeout_seconds: int = _EMBED_TIMEOUT_SECONDS,
        batch_size: int = _BATCH_CHUNK,
        concurrency: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.concurrency = max(1, concurrency)
        self._available: Optional[bool] = None

    # ── availability check ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if the Ollama service is reachable and the model is loaded."""
        if self._available is not None:
            return self._available
        try:
            resp = requests.get(f"{self.base_url}{_TAGS_ENDPOINT}", timeout=5)
            resp.raise_for_status()
            names = {m["name"] for m in resp.json().get("models", [])}
            # match by full name first, then by base name (before ":")
            base = self.model.split(":")[0]
            self._available = self.model in names or any(
                n.split(":")[0] == base for n in names
            )
            if not self._available:
                log.warning(
                    "Ollama model '%s' not found. Available: %s",
                    self.model,
                    sorted(names),
                )
        except Exception as exc:
            log.warning("Ollama not reachable at %s: %s", self.base_url, exc)
            self._available = False
        return self._available

    # ── embedding ─────────────────────────────────────────────────────────────

    def embed(self, text: str) -> Optional[list[float]]:
        """Embed a single string. Returns None on failure."""
        results = self.embed_batch([text])
        return results[0] if results else None

    def embed_batch(
        self,
        texts: list[str],
        chunk_size: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[Optional[list[float]]]:
        """Embed a list of strings using Ollama's batch /api/embed endpoint.

        Splits into chunks of `chunk_size` to keep individual requests
        within memory / timeout limits.  Returns a parallel list of vectors
        (None for any text that failed).
        """
        if not texts:
            return []

        effective_chunk_size = max(1, chunk_size or self.batch_size)
        chunks = [
            (start, texts[start : start + effective_chunk_size])
            for start in range(0, len(texts), effective_chunk_size)
        ]
        if progress_callback:
            progress_callback(0, len(texts))

        if self.concurrency <= 1 or len(chunks) <= 1:
            results: list[Optional[list[float]]] = []
            for start, chunk in chunks:
                log.info(
                    "Ollama embedding batch starting model=%s progress=%d/%d batch_size=%d timeout=%ss concurrency=%d",
                    self.model,
                    len(results),
                    len(texts),
                    len(chunk),
                    self.timeout_seconds,
                    self.concurrency,
                )
                chunk_vecs = self._call_embed(chunk)
                results.extend(chunk_vecs)
                if progress_callback:
                    progress_callback(len(results), len(texts))
            return results

        results_by_start: dict[int, list[Optional[list[float]]]] = {}
        completed = 0
        worker_count = min(self.concurrency, len(chunks))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            for start, chunk in chunks:
                log.info(
                    "Ollama embedding batch queued model=%s offset=%d/%d batch_size=%d timeout=%ss concurrency=%d",
                    self.model,
                    start,
                    len(texts),
                    len(chunk),
                    self.timeout_seconds,
                    worker_count,
                )
                futures[executor.submit(self._call_embed, chunk)] = (start, len(chunk))

            for future in as_completed(futures):
                start, chunk_len = futures[future]
                try:
                    results_by_start[start] = future.result()
                except Exception as exc:
                    log.warning("Ollama embed worker failed: %s", exc)
                    results_by_start[start] = [None] * chunk_len
                completed += chunk_len
                if progress_callback:
                    progress_callback(min(completed, len(texts)), len(texts))

        results = []
        for start, chunk in chunks:
            chunk_vecs = results_by_start.get(start)
            if chunk_vecs is None:
                chunk_vecs = [None] * len(chunk)
            results.extend(chunk_vecs)
        return results

    def _call_embed(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Embed a chunk of texts, retrying and splitting on failure.

        A failed request (typically a read timeout) does not silently drop all of
        its texts: a multi-text chunk is split in half and each half retried
        (smaller requests beat the timeout), and a single text is retried a few
        times with backoff before finally yielding None for just that one text.
        """
        try:
            return self._embed_once(texts)
        except Exception as exc:
            log.warning("Ollama embed request failed for %d text(s): %s", len(texts), exc)

        # Multi-text: split and retry each half — much cheaper than losing all.
        if len(texts) > 1:
            mid = len(texts) // 2
            return self._call_embed(texts[:mid]) + self._call_embed(texts[mid:])

        # Single text: back off and retry a bounded number of times.
        for attempt in range(1, _MAX_SINGLE_RETRIES + 1):
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            try:
                return self._embed_once(texts)
            except Exception as exc:
                log.warning(
                    "Ollama embed retry %d/%d failed: %s", attempt, _MAX_SINGLE_RETRIES, exc
                )
        return [None] * len(texts)

    def _embed_once(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Single HTTP call to /api/embed. Raises on transport/HTTP failure."""
        resp = requests.post(
            f"{self.base_url}{_EMBED_ENDPOINT}",
            json={"model": self.model, "input": texts},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        embeddings: list[list[float]] = resp.json().get("embeddings", [])

        if len(embeddings) != len(texts):
            log.warning(
                "Ollama returned %d embeddings for %d texts; padding with None",
                len(embeddings),
                len(texts),
            )

        result: list[Optional[list[float]]] = list(embeddings)
        while len(result) < len(texts):
            result.append(None)
        return result
