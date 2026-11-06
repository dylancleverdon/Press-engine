"""LLM clients.

Two roles (spec §4.5, §4.7):
* ``extract`` - small local model (Qwen 3B via Ollama / llama.cpp) for bulk JSON extraction.
* ``score``   - larger model for taste scoring, translation, openers and reply drafts.

Every client exposes ``json(system, prompt, schema)`` and ``text(system, prompt)``.
JSON output is always re-validated by the caller with Pydantic; anything that
doesn't validate is discarded.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from ..config import Config


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """The model server couldn't be reached or refused the request (not a bad answer)."""


class LLMClient(Protocol):
    name: str

    def json(self, system: str, prompt: str, schema: dict, *, max_tokens: int = 1024) -> dict: ...

    def text(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str: ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_json_text(text: str) -> dict:
    """Parse a model's JSON reply, tolerating code fences and leading chatter."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Model did not return JSON") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"Model returned invalid JSON: {e}") from None
    if not isinstance(value, dict):
        raise LLMError("Model returned JSON that is not an object")
    return value


def build_client(cfg: Config, role: str, http: httpx.Client | None = None) -> LLMClient | None:
    if role == "extract":
        provider, model, base, key = (
            cfg.extract_llm_provider, cfg.extract_llm_model, cfg.extract_llm_base_url, cfg.extract_llm_api_key,
        )
    else:
        provider, model, base, key = (
            cfg.score_llm_provider, cfg.score_llm_model, cfg.score_llm_base_url, cfg.score_llm_api_key,
        )
    if provider == "none" or not provider:
        return None
    if provider == "ollama":
        from .ollama import OllamaClient

        return OllamaClient(base or "http://localhost:11434", model or "qwen2.5:3b", http=http, num_ctx=cfg.extract_llm_num_ctx)
    if provider == "openai":
        from .openai_compat import OpenAICompatClient

        if not base:
            raise LLMError(f"PRESS_{role.upper()}_LLM_BASE_URL is required for the openai provider")
        return OpenAICompatClient(base, model or "default", api_key=key, http=http)
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model or "claude-opus-5-5", api_key=key, effort=cfg.score_llm_effort or None)
    raise LLMError(f"Unknown LLM provider: {provider}")
