"""CompanionPipeline — full request-to-response orchestration.

Orchestrates: game_state request -> prompt construction -> LLM inference
-> intent validation -> protocol response.

Designed to drop into the Spike 002 sidecar as a replacement for
deterministic_reply(), or run standalone for testing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ACTION_SCHEMA
from .llm_backend import LLMClient, get_client, init_client
from .prompt_engine import PromptEngine
from .intent_validator import IntentResult, validate_intent

LOGGER = logging.getLogger("pz-companion.pipeline")


@dataclass
class PipelineResult:
    """Complete result from one pipeline invocation."""

    intent_result: IntentResult
    elapsed_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    system_prompt: str = ""
    user_prompt: str = ""
    raw_llm_output: str = ""
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        """Return a compact summary dict for logging/display."""
        return {
            "action": self.intent_result.action,
            "intent": self.intent_result.intent,
            "valid": self.intent_result.valid,
            "confidence": self.intent_result.confidence,
            "reason": self.intent_result.reason,
            "elapsed_ms": self.elapsed_ms,
            "tokens": f"{self.prompt_tokens}+{self.completion_tokens}",
            "warnings": self.intent_result.warnings,
            "error": self.error,
        }


class CompanionPipeline:
    """Main companion decision pipeline.

    Usage::

        from llm_pipeline import CompanionPipeline

        pipe = CompanionPipeline(profile="cautious_survivor")
        # or with custom backend:
        pipe = CompanionPipeline(
            profile="aggressive_fighter",
            base_url="http://localhost:8000/v1",  # vLLM
            model="Qwen/Qwen2.5-0.5B-Instruct",
        )

        result = pipe.decide(request_json)
        print(result.summary())
    """

    def __init__(
        self,
        profile: str = "neutral",
        base_url: str | None = None,
        model: str | None = None,
        use_json_schema: bool = True,
        **llm_kwargs: Any,
    ) -> None:
        self.prompt_engine = PromptEngine(profile_key=profile)
        self.use_json_schema = use_json_schema

        # Initialize or reuse LLM client
        if base_url or model:
            kwargs: dict[str, Any] = {}
            if base_url:
                kwargs["base_url"] = base_url
            if model:
                kwargs["model"] = model
            kwargs.update(llm_kwargs)
            self.client = init_client(**kwargs)
        else:
            self.client = get_client()

        self._run_log: list[dict[str, Any]] = []

    def decide(self, request: dict[str, Any]) -> PipelineResult:
        """Run the full decision pipeline for one game state request.

        Args:
            request: Game state dict with trigger_type, state, events, etc.

        Returns:
            PipelineResult with validated intent and diagnostics.
        """
        start = time.perf_counter()
        result = PipelineResult(
            intent_result=IntentResult(),
            system_prompt=self.prompt_engine.build_system_prompt(),
            user_prompt=self.prompt_engine.build_user_prompt(request),
        )

        # Build messages
        messages = self.prompt_engine.build_messages(request)

        # Call LLM
        try:
            llm_result = self.client.chat(
                messages,
                json_schema=ACTION_SCHEMA if self.use_json_schema else None,
            )
            result.raw_llm_output = llm_result["content"]
            result.prompt_tokens = llm_result["prompt_tokens"]
            result.completion_tokens = llm_result["completion_tokens"]

            # Validate intent
            result.intent_result = validate_intent(
                llm_result["parsed"],
                raw_content=llm_result["content"],
            )

        except Exception as exc:
            result.error = str(exc)
            result.intent_result = IntentResult(
                valid=False,
                reason=f"Pipeline error: {exc}",
                warnings=[f"LLM call failed: {exc}"],
            )
            LOGGER.error("Pipeline error: %s", exc)

        result.elapsed_ms = int((time.perf_counter() - start) * 1000)

        # Log run
        self._run_log.append(result.summary())

        return result

    def decide_from_file(self, fixture_path: str | Path) -> PipelineResult:
        """Load a fixture JSON file and run the pipeline."""
        path = Path(fixture_path)
        with path.open("r", encoding="utf-8") as f:
            request = json.load(f)
        return self.decide(request)

    def batch_decide(
        self,
        requests: list[dict[str, Any]],
        *,
        labels: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run the pipeline on a batch of requests and return summary dicts.

        Useful for testing: pass labeled requests and compare outputs.
        """
        results = []
        for i, req in enumerate(requests):
            pr = self.decide(req)
            summary = pr.summary()
            summary["label"] = labels[i] if labels and i < len(labels) else f"request_{i}"
            results.append(summary)
        return results

    @property
    def run_log(self) -> list[dict[str, Any]]:
        """Return all run summaries from this pipeline instance."""
        return list(self._run_log)

    def clear_log(self) -> None:
        self._run_log.clear()
        self.prompt_engine.clear_history()
