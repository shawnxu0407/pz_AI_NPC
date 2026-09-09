"""Prompt construction engine for PZ Companion.

Builds system and user prompts from game state, companion profile,
and trigger context. Supports both player_command and event_batch triggers.
"""

from __future__ import annotations

from typing import Any

from .config import ACTION_SET, COMPANION_PROFILES


class PromptEngine:
    """Builds structured prompts for the companion decision pipeline.

    Args:
        profile_key: Key from COMPANION_PROFILES. Defaults to 'neutral'.
        action_set: Override the default ACTION_SET if needed.
    """

    def __init__(
        self,
        profile_key: str = "neutral",
        action_set: list[dict[str, str]] | None = None,
    ) -> None:
        self.profile = COMPANION_PROFILES.get(profile_key, COMPANION_PROFILES["neutral"])
        self.actions = action_set or ACTION_SET
        self._history: list[dict[str, str]] = []

    def set_profile(self, profile_key: str) -> None:
        """Switch companion personality mid-session."""
        self.profile = COMPANION_PROFILES.get(profile_key, COMPANION_PROFILES["neutral"])

    def add_history(self, role: str, content: str) -> None:
        """Append a turn to conversation history (for multi-turn context)."""
        self._history.append({"role": role, "content": content})
        # Keep history bounded to last 10 turns
        if len(self._history) > 10:
            self._history = self._history[-10:]

    def clear_history(self) -> None:
        self._history.clear()

    def build_system_prompt(self) -> str:
        """Build the system prompt with identity, rules, action set, and few-shot examples."""
        action_lines = "\n".join(
            f"- {a['name']}: {a['desc']}" for a in self.actions
        )

        personality_block = ""
        if self.profile["bias"] != "none":
            personality_block = f"""\n
Companion identity:
- Name: {self.profile['name']}
- Personality: {self.profile['personality']}
Let this personality subtly influence your decisions when multiple actions seem equally valid.
"""

        return f"""You are the decision-making module for a human survivor companion NPC in Project Zomboid.

Your job: given the current situation, choose **exactly one** action from the
fixed set below that best fits. You are answering a structured decision question,
not roleplaying or writing dialogue.
{personality_block}
Available actions:
{action_lines}

Rules:
1. Only choose one action name from the list above. Never invent a new action.
2. Fields you don't need (e.g. `wait` needs no coordinates) must be null — never make up values.
3. If the player's intent is unclear or information is insufficient, default to continue_follow.
4. The `reason` field is one short sentence explaining your choice for debugging.
5. When multiple threats exist, prioritize the closest/most dangerous one.
6. Player commands always take priority over autonomous event responses.

Examples:
Input: player says "follow me" -> Output: {{"action":"continue_follow","target_x":null,"target_y":null,"target_id":null,"reason":"player explicitly asked to keep following"}}
Input: player says "go grab that axe", axe_01 is nearby -> Output: {{"action":"pick_up_item","target_x":null,"target_y":null,"target_id":"axe_01","reason":"player specified a clear pickup target"}}
Input: event report "strange noise at (120,340)", no player command -> Output: {{"action":"investigate","target_x":120,"target_y":340,"target_id":null,"reason":"non-urgent but worth-checking event occurred"}}
Input: event "3 zombies in melee range", health 40% -> Output: {{"action":"retreat","target_x":null,"target_y":null,"target_id":null,"reason":"outnumbered and low health, fall back to player"}}
Input: event "zombie 5 tiles away, alone", health 90% -> Output: {{"action":"defend","target_x":null,"target_y":null,"target_id":null,"reason":"single threat at close range, healthy enough to engage"}}
"""

    def build_user_prompt(self, request: dict[str, Any]) -> str:
        """Build the user prompt from a game state request.

        Supports trigger_type: 'player_command', 'event_batch', 'periodic_tick'.
        """
        trigger_type = request.get("trigger_type", "unknown")
        state = request.get("state", {})

        # Build state summary
        state_lines = []
        state_lines.append(f"Health: {state.get('health', 'unknown')}")

        # Zombie summary
        zombies = state.get("nearby_zombies", [])
        if zombies:
            state_lines.append(f"Nearby zombies: {len(zombies)}")
            for i, z in enumerate(zombies[:5]):  # Cap at 5 for token budget
                state_lines.append(f"  zombie_{i}: ({z.get('x', '?')}, {z.get('y', '?')})")
        else:
            state_lines.append("Nearby zombies: 0")

        # Items summary
        items = state.get("nearby_items", [])
        if items:
            state_lines.append(f"Nearby items: {len(items)}")
            for item in items[:5]:
                state_lines.append(
                    f"  {item.get('id', '?')}: {item.get('name', '?')} at ({item.get('x', '?')}, {item.get('y', '?')})"
                )
        else:
            state_lines.append("Nearby items: none")

        # Companion state (if available)
        companion = state.get("companion", {})
        if companion:
            state_lines.append(f"Companion health: {companion.get('health', 'unknown')}")
            state_lines.append(f"Companion position: ({companion.get('x', '?')}, {companion.get('y', '?')})")
            if companion.get("current_action"):
                state_lines.append(f"Current action: {companion['current_action']}")

        # Player position (if available)
        player_pos = state.get("player_position", {})
        if player_pos:
            state_lines.append(f"Player position: ({player_pos.get('x', '?')}, {player_pos.get('y', '?')})")

        # Time/weather (if available)
        if state.get("time_of_day"):
            state_lines.append(f"Time: {state['time_of_day']}")
        if state.get("weather"):
            state_lines.append(f"Weather: {state['weather']}")

        state_summary = "\n".join(state_lines)

        # Build trigger-specific prompt
        if trigger_type == "player_command":
            return (
                f"[Triggered by player command]\n"
                f"Player said: \"{request.get('player_command', '')}\"\n\n"
                f"Current state:\n{state_summary}\n\n"
                f"Choose one action."
            )

        elif trigger_type == "event_batch":
            events = request.get("events", [])
            event_lines = "\n".join(
                f"- {e.get('type', 'unknown')}: {e.get('detail', '')}" for e in events
            )
            return (
                f"[Triggered by event report, no player command]\n"
                f"Recent events:\n{event_lines}\n\n"
                f"Current state:\n{state_summary}\n\n"
                f"Choose one action."
            )

        elif trigger_type == "periodic_tick":
            return (
                f"[Periodic autonomous check, no player command or event]\n\n"
                f"Current state:\n{state_summary}\n\n"
                f"Decide if any proactive action is warranted, or continue_follow."
            )

        else:
            return (
                f"[Unknown trigger: {trigger_type}]\n\n"
                f"Current state:\n{state_summary}\n\n"
                f"Choose one action."
            )

    def build_messages(self, request: dict[str, Any]) -> list[dict[str, str]]:
        """Build the full message list for LLM input.

        Returns [system, ...history, user] message sequence.
        """
        messages = [{"role": "system", "content": self.build_system_prompt()}]
        messages.extend(self._history)
        messages.append({"role": "user", "content": self.build_user_prompt(request)})
        return messages
