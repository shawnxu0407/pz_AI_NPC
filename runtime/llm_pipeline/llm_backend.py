"""LLM backend abstraction — OpenAI-compatible client for llama.cpp / vLLM / any server.

Follows the init_client(base_url=...) pattern so swapping backends is trivial.
Pure stdlib: only uses urllib (no openai / requests dependency).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

from .config import (
    ACTION_SCHEMA,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT,
)

LOGGER = logging.getLogger("pz-companion.llm")


@dataclass
class LLMClient:
    """Lightweight OpenAI-compatible chat-completion client.

    Works with any server that exposes ``/v1/chat/completions``:
    llama.cpp, vLLM, Ollama, text-generation-inference, etc.
    """

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT
    _stats: dict[str, Any] = field(default_factory=lambda: {"calls": 0, "total_ms": 0, "errors": 0})

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request and return the parsed result.

        Returns:
            dict with keys: content (str), parsed (dict|None), elapsed_ms (int),
                            prompt_tokens (int), completion_tokens (int)
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }

        # Constrained JSON decoding if schema provided
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "schema": json_schema,
            }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            self._stats["errors"] += 1
            raise RuntimeError(
                f"LLM server unreachable at {self.endpoint}. "
                f"Ensure llama-server / vLLM is running. Error: {exc}"
            ) from exc

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        self._stats["calls"] += 1
        self._stats["total_ms"] += elapsed_ms

        choice = body["choices"][0]
        content = choice["message"]["content"]
        usage = body.get("usage", {})

        # Try to parse JSON from content
        parsed = None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning("LLM returned non-JSON content: %s", content[:200])

        return {
            "content": content,
            "parsed": parsed,
            "elapsed_ms": elapsed_ms,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }

    @property
    def stats(self) -> dict[str, Any]:
        """Return cumulative call statistics for diagnostics."""
        avg_ms = self._stats["total_ms"] / max(1, self._stats["calls"])
        return {**self._stats, "avg_ms": round(avg_ms, 1)}

    def reset_stats(self) -> None:
        self._stats = {"calls": 0, "total_ms": 0, "errors": 0}


# ---------------------------------------------------------------------------
# Module-level convenience (matches existing init_client pattern)
# ---------------------------------------------------------------------------
_client: LLMClient | None = None


def init_client(
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    **kwargs: Any,
) -> LLMClient:
    """Initialize the module-level LLM client. Swapping backends = changing base_url.

    Examples:
        # llama.cpp (default)
        init_client()

        # vLLM
        init_client(base_url="http://localhost:8000/v1", model="Qwen/Qwen2.5-0.5B-Instruct")

        # Ollama
        init_client(base_url="http://localhost:11434/v1", model="qwen2.5:0.5b")
    """
    global _client
    _client = LLMClient(base_url=base_url, model=model, **kwargs)
    LOGGER.info("LLM client initialized: %s model=%s", base_url, model)
    return _client


def get_client() -> LLMClient:
    """Return the current client, auto-initializing with defaults if needed."""
    global _client
    if _client is None:
        _client = init_client()
    return _client


def call_llm(
    messages: list[dict[str, str]],
    *,
    json_schema: dict | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convenience wrapper: call the module-level client."""
    return get_client().chat(messages, json_schema=json_schema, **kwargs)
