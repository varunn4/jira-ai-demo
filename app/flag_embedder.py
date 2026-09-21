"""BGE-M3 hybrid embedder using FlagEmbedding.

Produces BOTH dense (1024-dim) and sparse (lexical) vectors from a single
model pass — enabling Qdrant's RRF hybrid search, which outperforms
dense-only cosine search by combining semantic meaning (dense) with exact
term matching (sparse).

Why not Ollama for this?
  Ollama's /api/embed returns only dense vectors.  BGE-M3's sparse output
  requires the FlagEmbedding library, which wraps the raw PyTorch model.

Sparse vector format used by Qdrant:
  SparseVector(indices=[token_id, ...], values=[weight, ...])
  Token IDs come directly from the model's XLM-RoBERTa tokenizer vocab.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

_lock = threading.Lock()
_model: Any = None          # BGEM3FlagModel singleton
_vocab: dict[str, int] = {} # tokenizer vocab cache {token: id}


def _load_model() -> Any:
    global _model, _vocab
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding not installed. Add it to requirements.txt and run: "
                "pip install FlagEmbedding"
            ) from exc

        log.info("Loading BAAI/bge-m3 via FlagEmbedding (first call — may take 30-60s)…")
        m = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
        _vocab = m.tokenizer.get_vocab()  # {token_str: token_id}
        _model = m
        log.info("BGE-M3 model ready (%d vocab tokens)", len(_vocab))
        return _model


def _sparse_to_qdrant(lexical: dict[str, float]) -> tuple[list[int], list[float]]:
    """Convert FlagEmbedding lexical_weights → (indices, values) for Qdrant SparseVector.

    lexical_weights keys are decoded token strings (e.g. '▁repayment').
    We map them back to integer vocab IDs using the tokenizer's vocab dict.
    Tokens not found in vocab (very rare) are skipped.
    """
    indices: list[int] = []
    values: list[float] = []
    seen: set[int] = set()
    for token, weight in lexical.items():
        idx = _vocab.get(token)
        if idx is not None and idx not in seen:
            indices.append(idx)
            values.append(float(weight))
            seen.add(idx)
    return indices, values


class BGEM3Embedder:
    """Thin wrapper around BGEM3FlagModel with lazy loading and graceful fallback."""

    def is_available(self) -> bool:
        try:
            import FlagEmbedding  # noqa: F401
            return True
        except ImportError:
            return False

    def encode_batch(
        self,
        texts: list[str],
        batch_size: int = 8,
    ) -> list[dict[str, Any]]:
        """
        Encode a list of texts and return:
          [{"dense": list[float], "sparse_indices": list[int], "sparse_values": list[float]}, ...]

        Falls back to dense-only if sparse encoding fails.
        """
        model = _load_model()
        output = model.encode(
            texts,
            batch_size=batch_size,
            max_length=512,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense_vecs = output["dense_vecs"]          # numpy array (N, 1024)
        lexical_list = output["lexical_weights"]    # list[dict[str, float]]

        results = []
        for i, text in enumerate(texts):
            d = dense_vecs[i].tolist()
            lex = lexical_list[i] if i < len(lexical_list) else {}
            idx, val = _sparse_to_qdrant(lex)
            results.append({
                "dense": d,
                "sparse_indices": idx,
                "sparse_values": val,
            })
        return results

    def encode_one(self, text: str) -> Optional[dict[str, Any]]:
        results = self.encode_batch([text])
        return results[0] if results else None
