"""Intent validation for PZ Companion LLM output.

Validates LLM responses against the finite action set and protocol constraints.
Invalid output is converted to safe fallbacks rather than crashing the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import ACTION_NAMES, ACTION_TO_INTENT, INTENT_VOCABULARY

LOGGER = logging.getLogger("pz-companion.validator")


@dataclass
class IntentResult:
    """Validated intent output from the pipeline.

    Attributes:
        valid: Whether the LLM output passed all validation checks.
        action: The chosen action name (from ACTION_SET).
        intent: Protocol v1 intent (from INTENT_VOCABULARY).
        target_x: Optional target x coordinate.
        target_y: Optional target y coordinate.
        target_id: Optional target item/object identifier.
        reason: LLM's reasoning for the choice.
        confidence: Validation confidence (1.0 if valid, 0.0 if fallback).
        warnings: List of validation warnings.
        raw_output: Original LLM output for diagnostics.
    """

    valid: bool = False
    action: str = "continue_follow"
    intent: str = "FOLLOW"
    target_x: float | None = None
    target_y: float | None = None
    target_id: str | None = None
    reason: str = ""
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    raw_output: Any = None

    def to_protocol_response(self, request_id: str, processing_ms: int) -> dict[str, Any]:
        """Convert to a protocol v1 response dict for the sidecar."""
        return {
            "protocolVersion": 1,
            "requestId": request_id,
            "status": "ok" if self.valid else "error",
            "speech": self.reason,
            "intent": self.intent,
            "confidence": self.confidence,
            "parameters": self._build_parameters(),
            "diagnostics": {
                "runtimeMode": "llm-pipeline",
                "action": self.action,
                "processingMs": processing_ms,
                "warnings": self.warnings,
            },
        }

    def _build_parameters(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.target_x is not None:
            params["target_x"] = self.target_x
        if self.target_y is not None:
            params["target_y"] = self.target_y
        if self.target_id is not None:
            params["target_id"] = self.target_id
        return params


def validate_intent(parsed: dict[str, Any] | None, *, raw_content: str = "") -> IntentResult:
    """Validate LLM output and return a safe IntentResult.

    Any validation failure produces a safe fallback (continue_follow/FOLLOW)
    rather than propagating bad data to game logic.
    """
    result = IntentResult(raw_output=raw_content)

    # --- Parse failure ---
    if parsed is None:
        result.warnings.append("LLM output was not valid JSON")
        result.reason = "LLM output parse failure; defaulting to follow"
        LOGGER.warning("Validation failed: non-JSON output")
        return result

    # --- Action validation ---
    action = parsed.get("action")
    if not isinstance(action, str) or action not in ACTION_NAMES:
        result.warnings.append(f"Unknown action: {action!r}; expected one of {ACTION_NAMES}")
        result.reason = f"Invalid action '{action}'; defaulting to follow"
        LOGGER.warning("Validation failed: unknown action %r", action)
        return result

    result.action = action
    result.intent = ACTION_TO_INTENT.get(action, "NONE")
    result.valid = True
    result.confidence = 1.0

    # --- Reason ---
    reason = parsed.get("reason", "")
    if isinstance(reason, str) and reason:
        result.reason = reason
    else:
        result.warnings.append("Missing or empty reason field")
        result.reason = f"Action: {action}"

    # --- Target coordinates ---
    target_x = parsed.get("target_x")
    target_y = parsed.get("target_y")

    if action in ("move_to", "investigate"):
        # These actions require coordinates
        if _is_number(target_x) and _is_number(target_y):
            result.target_x = float(target_x)
            result.target_y = float(target_y)
        else:
            result.warnings.append(f"{action} requires target_x/target_y but got ({target_x}, {target_y})")
            result.confidence = 0.5
    else:
        # Other actions should have null coordinates
        if _is_number(target_x) or _is_number(target_y):
            result.warnings.append(f"{action} should not have coordinates; ignoring ({target_x}, {target_y})")
        result.target_x = None
        result.target_y = None

    # --- Target ID ---
    target_id = parsed.get("target_id")
    if action == "pick_up_item":
        if isinstance(target_id, str) and target_id:
            result.target_id = target_id
        else:
            result.warnings.append("pick_up_item requires target_id but none provided")
            result.confidence = 0.5
    else:
        if target_id is not None and target_id != "":
            result.warnings.append(f"{action} should not have target_id; ignoring '{target_id}'")
        result.target_id = None

    return result


def _is_number(value: Any) -> bool:
    """Check if a value is a valid number (not None, not NaN)."""
    if value is None:
        return False
    try:
        f = float(value)
        return f == f  # NaN check
    except (TypeError, ValueError):
        return False
