"""Configuration constants for the PZ Companion LLM pipeline.

Centralizes the action set, intent vocabulary, JSON schema, and model defaults.
Editing ACTION_SET automatically propagates to prompts and validation.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Action set — the complete list of actions the companion NPC can execute.
# Adding a new action only requires editing this list; the system prompt,
# JSON schema, and validator all derive from it.
# ---------------------------------------------------------------------------
ACTION_SET: list[dict[str, str]] = [
    {
        "name": "continue_follow",
        "desc": "Nothing changes, keep following the player",
    },
    {
        "name": "move_to",
        "desc": "Move to the given coordinates (target_x, target_y)",
    },
    {
        "name": "investigate",
        "desc": "Go check out something that caught attention (a noise, a suspicious spot); needs target_x/target_y",
    },
    {
        "name": "pick_up_item",
        "desc": "Pick up an item; needs target_id identifying the item",
    },
    {
        "name": "retreat",
        "desc": "Fall back to the player's side; use when there's a sensed threat but not yet active combat",
    },
    {
        "name": "wait",
        "desc": "Stay put and take no action",
    },
    {
        "name": "defend",
        "desc": "Engage nearby threat in combat; use when zombies are in immediate melee range",
    },
    {
        "name": "loot",
        "desc": "Search a nearby container or area for useful items",
    },
    {
        "name": "barricade",
        "desc": "Fortify the current position (board windows, block doors)",
    },
]

# Derived constants
ACTION_NAMES: list[str] = [a["name"] for a in ACTION_SET]

# Protocol v1 intent vocabulary (maps to sidecar protocol)
INTENT_VOCABULARY: set[str] = {
    "NONE",
    "FOLLOW",
    "WAIT",
    "GUARD",
    "RETREAT",
    "COLLECT_RESOURCE",
    "MOVE_ITEMS",
    "COOK",
    "LOOT",
    "DEFEND",
}

# Action-to-intent mapping for protocol v1 bridge
ACTION_TO_INTENT: dict[str, str] = {
    "continue_follow": "FOLLOW",
    "move_to": "NONE",
    "investigate": "NONE",
    "pick_up_item": "COLLECT_RESOURCE",
    "retreat": "RETREAT",
    "wait": "WAIT",
    "defend": "DEFEND",
    "loot": "LOOT",
    "barricade": "GUARD",
}

# JSON schema for constrained decoding (llama.cpp / vLLM)
ACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTION_NAMES},
        "target_x": {"type": ["number", "null"]},
        "target_y": {"type": ["number", "null"]},
        "target_id": {"type": ["string", "null"]},
        "reason": {
            "type": "string",
            "description": "One short sentence explaining why this action was chosen",
        },
    },
    "required": ["action", "target_x", "target_y", "target_id", "reason"],
}

# ---------------------------------------------------------------------------
# Companion personality profiles
# ---------------------------------------------------------------------------
COMPANION_PROFILES: dict[str, dict] = {
    "cautious_survivor": {
        "name": "Elena",
        "personality": "Cautious and methodical. Prefers to avoid conflict when possible. Prioritizes safety and resource conservation.",
        "bias": "retreat_on_threat",
    },
    "aggressive_fighter": {
        "name": "Marcus",
        "personality": "Bold and combat-ready. Prefers to confront threats head-on. Will stand ground unless badly outnumbered.",
        "bias": "defend_on_threat",
    },
    "resourceful_scavenger": {
        "name": "Kit",
        "personality": "Resourceful and opportunistic. Always scanning for useful items. Will investigate noises hoping to find supplies.",
        "bias": "investigate_and_loot",
    },
    "neutral": {
        "name": "Companion",
        "personality": "Balanced and adaptable. Follows the player's lead without strong personal preferences.",
        "bias": "none",
    },
}

# ---------------------------------------------------------------------------
# Model defaults
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "http://localhost:8080/v1"  # llama.cpp server
DEFAULT_MODEL = "qwen2.5-0.5b-instruct-q4_k_m"  # from manifest.example.json
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 256
DEFAULT_TIMEOUT = 10.0
