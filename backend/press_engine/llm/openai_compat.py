"""Any OpenAI-compatible /v1/chat/completions server (llama.cpp, LM Studio, vLLM, ...)."""

from __future__ import annotations

import httpx

from . import LLMError, LLMUnavailable, parse_json_text


class OpenAICompatClient:
    def __init__(self, base_url: str, model: str, *, api_key: str | None = None, http: httpx.Client | None = None, timeout: float = 180):
        base = base_url.rstrip("/")
        self.url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        self.model = model
        self.name = f"openai-compat:{model}"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = http or httpx.Client(timeout=timeout)

    def _complete(self, system: str, prompt: str, response_format: dict | None, max_tokens: int) -> str:
        body: dict = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if response_format:
            body["response_format"] = response_format
        try:
            r = self.http.post(self.url, json=body, headers=self.headers)
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"LLM server unreachable at {self.url}: {e}") from e
        if r.status_code != 200:
            raise LLMUnavailable(f"LLM server error {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"Unexpected LLM response: {r.text[:300]}") from e

    def json(self, system: str, prompt: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        fmt = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}
        return parse_json_text(self._complete(system, prompt, fmt, max_tokens))

    def text(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        return self._complete(system, prompt, None, max_tokens).strip()
