"""LLM client implementations and provider selection.

This file defines the shared LLM client interface, provider-backed clients,
a mock client for local dry runs, and the factory that chooses the correct
client based on application settings. The system prompt contains stable
instructions, while the user message contains the ticket JSON.
"""

import dataclasses
import json
import logging
import re
from typing import Any, Optional, Protocol

from app.config import Settings
from app.exceptions import LLMConfigurationError

log = logging.getLogger(__name__)

# OpenAI reasoning models (o-series, gpt-5) drop `temperature` and use
# `max_completion_tokens` instead of `max_tokens`; standard chat models
# (gpt-4.1, gpt-4o, …) take the classic params.
_OPENAI_REASONING = re.compile(r"^(o[1-9]|gpt-5)")
# Model ids that should route to the OpenAI client rather than Anthropic.
_OPENAI_MODEL = re.compile(r"^(gpt|o[1-9]|chatgpt)", re.I)

# Claude 5 models reject the `temperature` parameter (it is deprecated for them).
# Match the Claude 5 family — "claude-<family>-5" optionally followed by a date —
# so we omit temperature for them but keep it for 4.x models. Note that
# "claude-haiku-4-5-…" is Haiku 4.5, NOT a 5 model, and must not match.
_CLAUDE_5_MODEL = re.compile(r"^claude-[a-z]+-5(?:$|-)")


def _supports_temperature(model: Optional[str]) -> bool:
    return not _CLAUDE_5_MODEL.match(model or "")


class LLMClient(Protocol):
    def complete(self, system_prompt: str, user_message: str, *,
                 max_tokens: int = 4096,
                 images: Optional[list[dict[str, Any]]] = None) -> str:
        """Return the model output for a system prompt plus user message.

        `images`, when given, are provider-native image content blocks appended
        to the user turn (multimodal). Text-only providers ignore them.
        """


class OpenAILLMClient:
    def __init__(self, settings: Settings, *, timeout_override: int | None = None) -> None:
        self.settings = settings

        if not settings.openai_api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMConfigurationError(
                "The 'openai' package is required. Install project dependencies first."
            ) from exc

        base_url = settings.openai_base_url
        provider = settings.llm_provider.lower().strip()
        if not base_url:
            if provider == "groq":
                base_url = "https://api.groq.com/openai/v1"
            elif provider == "gemini":
                base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"

        client_kwargs: dict[str, Any] = {
            "api_key": settings.openai_api_key,
            "timeout": timeout_override if timeout_override is not None else settings.llm_timeout_seconds,
        }
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = OpenAI(**client_kwargs)

    def complete(self, system_prompt: str, user_message: str, *,
                 max_tokens: int = 4096,
                 images: Optional[list[dict[str, Any]]] = None) -> str:
        # Image blocks here are Anthropic-native; the OpenAI path stays text-only.
        model = self.settings.llm_model
        log.info("OpenAI LLM call: model=%s input_chars=%d max_tokens=%d",
                 model, len(user_message), max_tokens)
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        if _OPENAI_REASONING.match(model or ""):
            kwargs["max_completion_tokens"] = max_tokens  # reasoning: no temperature
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = 0.1
        response = self.client.chat.completions.create(**kwargs)

        message = response.choices[0].message.content
        usage = getattr(response, "usage", None)
        if usage:
            log.info(
                "OpenAI LLM response: prompt_tokens=%s completion_tokens=%s",
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
            )
        return message or ""


class AnthropicLLMClient:
    def __init__(self, settings: Settings, *, timeout_override: int | None = None) -> None:
        self.settings = settings

        if not settings.anthropic_api_key:
            raise LLMConfigurationError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise LLMConfigurationError(
                "The 'anthropic' package is required. Install project dependencies first."
            ) from exc

        self.client = Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=timeout_override if timeout_override is not None else settings.llm_timeout_seconds,
        )

    def complete(self, system_prompt: str, user_message: str, *,
                 max_tokens: int = 4096,
                 images: Optional[list[dict[str, Any]]] = None) -> str:
        # With images, the user turn becomes a multimodal content array: the text
        # first, then each image block (base64). Without, it stays a plain string.
        if images:
            content: Any = [{"type": "text", "text": user_message}, *images]
        else:
            content = user_message
        log.info("Anthropic LLM call: model=%s input_chars=%d max_tokens=%d images=%d",
                 self.settings.llm_model, len(user_message), max_tokens, len(images or []))
        create_kwargs: dict[str, Any] = dict(
            model=self.settings.llm_model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        if _supports_temperature(self.settings.llm_model):
            create_kwargs["temperature"] = 0.1  # deprecated on Claude 5 — omit there
        response = self.client.messages.create(**create_kwargs)

        usage = getattr(response, "usage", None)
        if usage:
            log.info(
                "Anthropic LLM response: input_tokens=%s output_tokens=%s",
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
            )
        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        )


class MockLLMClient:
    def complete(self, system_prompt: str, user_message: str, *,
                 max_tokens: int = 4096,
                 images: Optional[list[dict[str, Any]]] = None) -> str:
        log.info("MockLLMClient.complete called (no external API call)")
        low_sys = (system_prompt or "").lower()
        low_usr = (user_message or "").lower()

        # If downstream expects test case generation:
        if "test case" in low_sys or "test cases" in low_usr or "gherkin" in low_sys:
            return (
                "### 🧪 Generated Test Suite (Local / Dynamic Fallback)\n\n"
                "#### TC-01: Happy Path Functional Validation\n"
                "- **Type**: Functional / Positive\n"
                "- **Priority**: High\n"
                "- **Preconditions**: Target service is operational; authentication token is valid.\n"
                "- **Steps**:\n"
                "  1. Send request with valid required parameters according to ticket specifications.\n"
                "  2. Verify HTTP 200 OK response with correct payload structure.\n"
                "  3. Confirm database state updates correctly.\n"
                "- **Expected Result**: Operation succeeds with expected business logic outcomes.\n\n"
                "#### TC-02: Boundary & Input Validation\n"
                "- **Type**: Negative / Validation\n"
                "- **Priority**: Medium\n"
                "- **Preconditions**: Service is accessible.\n"
                "- **Steps**:\n"
                "  1. Pass empty, null, and out-of-range parameters.\n"
                "  2. Verify input validation triggers immediate rejection.\n"
                "- **Expected Result**: System returns HTTP 400 Bad Request with a clear validation error message.\n\n"
                "#### TC-03: Security & Access Control\n"
                "- **Type**: Security\n"
                "- **Priority**: High\n"
                "- **Preconditions**: Unauthenticated or unauthorized user session.\n"
                "- **Steps**:\n"
                "  1. Attempt to execute the action without authorization headers.\n"
                "- **Expected Result**: System returns HTTP 401/403 Forbidden without exposing stack traces.\n\n"
                "#### TC-04: Regression & Concurrent Load\n"
                "- **Type**: Regression / Non-Functional\n"
                "- **Priority**: Medium\n"
                "- **Preconditions**: Concurrent worker threads initialized.\n"
                "- **Steps**:\n"
                "  1. Execute concurrent operations simultaneously.\n"
                "  2. Verify idempotency and data integrity.\n"
                "- **Expected Result**: No race conditions or deadlocks occur."
            )

        # If downstream expects JSON:
        if "json" in low_sys or "json" in low_usr:
            return json.dumps(
                {
                    "mock": True,
                    "status": "success",
                    "intent": "ticket_question",
                    "reply": "Mock LLM response generated successfully for local testing.",
                    "notes": ["No external LLM API key required in local test mode."],
                },
                indent=2,
            )

        return (
            "Mock LLM Output: Request processed successfully in local test mode.\n"
            "Configure an API Key (Groq, OpenAI, Gemini, or Anthropic) in Settings to enable live LLM model generation."
        )


def build_llm_client(settings: Settings, *, timeout_override: int | None = None) -> LLMClient:
    scoped = settings
    # Dynamically resolve database settings overrides if configured
    try:
        from app.app_settings import get_all_settings
        db_items = get_all_settings(settings)
        import psycopg2
        from psycopg2.extras import RealDictCursor
        with psycopg2.connect(settings.database_url) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT key, value FROM app_user_settings WHERE key IN ('llm_provider', 'llm_model', 'openai_api_key', 'openai_base_url', 'anthropic_api_key', 'anthropic_model');")
                for r in cur.fetchall():
                    if r.get("key") and r.get("value"):
                        db_items[r["key"]] = r["value"]

        updates: dict[str, Any] = {}
        if db_items.get("llm_provider"):
            updates["llm_provider"] = db_items["llm_provider"]
        if db_items.get("llm_model"):
            updates["llm_model"] = db_items["llm_model"]
        if db_items.get("openai_api_key"):
            updates["openai_api_key"] = db_items["openai_api_key"]
        if db_items.get("openai_base_url") is not None:
            updates["openai_base_url"] = db_items["openai_base_url"]
        if db_items.get("anthropic_api_key"):
            updates["anthropic_api_key"] = db_items["anthropic_api_key"]
        if db_items.get("anthropic_model"):
            updates["anthropic_model"] = db_items["anthropic_model"]
        if updates:
            scoped = dataclasses.replace(settings, **updates)
    except Exception:
        pass

    provider = scoped.llm_provider.lower().strip()
    log.info("Building LLM client: provider=%s model=%s timeout=%s", provider, scoped.llm_model,
             timeout_override if timeout_override is not None else scoped.llm_timeout_seconds)

    if provider in ("openai", "groq", "gemini"):
        if not scoped.openai_api_key:
            log.warning("No OPENAI_API_KEY configured for provider=%s; using MockLLMClient", provider)
            return MockLLMClient()
        return OpenAILLMClient(scoped, timeout_override=timeout_override)

    if provider == "anthropic":
        if not scoped.anthropic_api_key:
            if scoped.openai_api_key:
                log.info("No ANTHROPIC_API_KEY configured, but OPENAI_API_KEY found. Routing to OpenAI client.")
                return OpenAILLMClient(scoped, timeout_override=timeout_override)
            log.warning("No ANTHROPIC_API_KEY configured for provider=anthropic; using MockLLMClient")
            return MockLLMClient()
        return AnthropicLLMClient(scoped, timeout_override=timeout_override)

    if provider == "mock":
        log.warning("LLM_PROVIDER=mock — no real LLM calls will be made")
        return MockLLMClient()

    log.warning("Unknown or unconfigured provider %s; using MockLLMClient", provider)
    return MockLLMClient()


def build_client_for_model(
    settings: Settings, model: str, *, timeout_override: int | None = None
) -> LLMClient:
    """Build the provider client that owns a specific model id, and pin it.

    Lets a phase pick a model independent of the global LLM_PROVIDER — e.g. RCA
    synthesis on a GPT model while the rest of the app stays on Anthropic. Routes
    gpt-*/o-series ids to OpenAI, everything else to Anthropic.
    """
    scoped = dataclasses.replace(settings, llm_model=model)
    if _OPENAI_MODEL.match(model or ""):
        if not settings.openai_api_key:
            return MockLLMClient()
        return OpenAILLMClient(scoped, timeout_override=timeout_override)
    if not settings.anthropic_api_key:
        if settings.openai_api_key:
            return OpenAILLMClient(scoped, timeout_override=timeout_override)
        return MockLLMClient()
    return AnthropicLLMClient(scoped, timeout_override=timeout_override)
