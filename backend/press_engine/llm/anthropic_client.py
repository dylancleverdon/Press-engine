"""Claude as the larger scoring / translation / drafting model (optional).

Install with ``pip install 'press-engine[anthropic]'`` and set ANTHROPIC_API_KEY
(or PRESS_SCORE_LLM_API_KEY). JSON replies use structured outputs
(``output_config.format``), and server-side refusal fallbacks are enabled so a
declined request is retried on Anthropic's recommended fallback model.
"""

from __future__ import annotations

import json

from . import LLMError, LLMUnavailable

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    def __init__(self, model: str = "claude-opus-5-5", *, api_key: str | None = None, effort: str | None = None):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - optional dependency
            raise LLMError("The anthropic package is not installed: pip install 'press-engine[anthropic]'") from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.name = f"anthropic:{model}"
        self.effort = effort

    def _create(self, system: str, prompt: str, output_format: dict | None, max_tokens: int):
        anthropic = self._anthropic
        output_config: dict = {}
        if self.effort:
            output_config["effort"] = self.effort
        if output_format:
            output_config["format"] = output_format
        kwargs: dict = {
            "model": self.model,
            "max_tokens": max(max_tokens, 16000),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if output_config:
            kwargs["output_config"] = output_config
        try:
            response = self.client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMUnavailable(f"Anthropic rate limit: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMUnavailable(f"Anthropic connection error: {e}") from e
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude hit max_tokens before finishing")
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise LLMError("Claude returned no text")
        return text

    def json(self, system: str, prompt: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        text = self._create(system, prompt, {"type": "json_schema", "schema": schema}, max_tokens)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Claude returned invalid JSON: {e}") from e
        if not isinstance(value, dict):
            raise LLMError("Claude returned JSON that is not an object")
        return value

    def text(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        return self._create(system, prompt, None, max_tokens).strip()
