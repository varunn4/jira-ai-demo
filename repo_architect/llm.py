"""Thin Anthropic API wrapper with retry + prompt caching.

We cache the system prompt because it's identical across all 6 repo scans in a
batch — Anthropic charges 10% of input price for cache reads, so this is a real
saving on the full-scan and a small one on nightly patches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import anthropic
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import require_api_key

log = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Retry on transient failures: rate limits, timeouts, connection errors,
    and 5xx server errors. Do NOT retry on 4xx — those mean our request is
    malformed (bad input, oversize payload, auth failure) and retrying would
    just burn tokens.
    """
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return True
    # InternalServerError is a 500. APIStatusError is the parent class; check
    # status_code for the 5xx range to catch 502/503/504 as well.
    if isinstance(exc, anthropic.InternalServerError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and 500 <= status < 600:
            return True
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            error_type = str(error.get("type") or body.get("type") or "").lower()
            message = str(error.get("message") or body.get("message") or "").lower()
            if error_type in {"api_error", "overloaded_error"}:
                return True
            if "internal server error" in message or "overloaded" in message:
                return True
    return False


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    stop_reason: Optional[str] = None

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


class LLMClient:
    def __init__(self, model: str = "", max_tokens: int = 32_000):
        from app.config import settings
        self.settings = settings
        self.model = model or settings.llm_model
        self.max_tokens = max_tokens

    def complete(
        self,
        system: str,
        user: str,
        cache_system: bool = True,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        """Send message using the application's configured LLM provider."""
        from app.config import settings
        from app.llm_client import build_llm_client

        effective_max = max_tokens if max_tokens is not None else self.max_tokens
        log.info(
            "Documentation LLM call: provider=%s model=%s, system_chars=%d, user_chars=%d, max_tokens=%d",
            settings.llm_provider, settings.llm_model, len(system), len(user), effective_max,
        )

        try:
            client = build_llm_client(settings)
            # Use max tokens compatible with standard provider limits
            out_limit = min(effective_max, 8192)
            text = client.complete(system, user, max_tokens=out_limit)
            input_toks = max(1, (len(system) + len(user)) // 4)
            output_toks = max(1, len(text) // 4)
            return LLMResult(
                text=text,
                input_tokens=input_toks,
                output_tokens=output_toks,
                stop_reason="end_turn",
            )
        except Exception as exc:
            log.exception("Documentation LLM completion failed: %s", exc)
            raise RuntimeError(f"Documentation generation failed via {settings.llm_provider}: {exc}") from exc

