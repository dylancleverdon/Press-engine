"""Ollama chat API with JSON-schema constrained output."""

from __future__ import annotations

import httpx

from . import LLMError, LLMUnavailable, parse_json_text


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, http: httpx.Client | None = None, num_ctx: int = 8192, timeout: float = 180):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.num_ctx = num_ctx
        self.http = http or httpx.Client(timeout=timeout)

    def _chat(self, system: str, prompt: str, fmt: dict | None, max_tokens: int) -> str:
        body: dict = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": max_tokens},
        }
        if fmt is not None:
            body["format"] = fmt
        try:
            r = self.http.post(f"{self.base_url}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"Ollama unreachable at {self.base_url}: {e}") from e
        if r.status_code != 200:
            raise LLMUnavailable(f"Ollama error {r.status_code}: {r.text[:300]}")
        content = (r.json().get("message") or {}).get("content", "")
        if not content:
            raise LLMError("Ollama returned an empty response")
        return content

    def json(self, system: str, prompt: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        return parse_json_text(self._chat(system, prompt, schema, max_tokens))

    def text(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        return self._chat(system, prompt, None, max_tokens).strip()
