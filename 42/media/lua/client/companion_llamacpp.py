"""
PZ Companion — local llama.cpp server inference test script

Usage:
    python companion_llm.py --fixture fixtures/player_command_follow.json
    python companion_llm.py --fixture fixtures/event_batch_zombies.json --model qwen2.5:1.5b

Does not depend on Project Zomboid itself — just feed it a hand-written
state JSON file and see what the model outputs. This makes it easy to
iterate on the prompt / schema quickly without launching the game and
walking through the full IPC pipeline every time.
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

LLAMA_CPP_URL = "http://localhost:8080/v1/chat/completions"

# ---------------------------------------------------------------------------
# 1. Fixed action set
# ---------------------------------------------------------------------------
# This is the complete list of actions the companion NPC can execute.
# Adding a new action only requires editing this list (plus the Lua-side
# executor) — the system prompt and the JSON schema are both generated
# from it, so there's no second place to keep in sync.
ACTION_SET = [
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
]

ACTION_NAMES = [a["name"] for a in ACTION_SET]

# ---------------------------------------------------------------------------
# 2. JSON schema (passed to Ollama's `format` parameter for constrained decoding)
# ---------------------------------------------------------------------------
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTION_NAMES},
        "target_x": {"type": ["number", "null"]},
        "target_y": {"type": ["number", "null"]},
        "target_id": {"type": ["string", "null"]},
        "reason": {
            "type": "string",
            "description": "One short sentence explaining why this action was chosen, for debugging",
        },
    },
    "required": ["action", "target_x", "target_y", "target_id", "reason"],
}

# ---------------------------------------------------------------------------
# 3. System prompt (fixed part: identity + rules + action set + few-shot)
# ---------------------------------------------------------------------------
def build_system_prompt() -> str:
    action_lines = "\n".join(
        f"- {a['name']}: {a['desc']}" for a in ACTION_SET
    )

    return f"""You are the decision-making module for a human survivor companion NPC in Project Zomboid.

Your job: given the current situation, choose **exactly one** action from the
fixed set below that best fits. You must not output anything outside this
action set — you are answering a multiple-choice question, not roleplaying
or writing dialogue.

Available actions:
{action_lines}

Rules:
1. Only choose one action name from the list above. Never invent a new action.
2. Fields you don't need (e.g. `wait` needs no coordinates) must be null — never make up values.
3. If the player's intent is unclear, or there isn't enough information to decide, default to continue_follow rather than guessing.
4. The `reason` field is one short sentence explaining your choice, so a human can sanity-check your reasoning.

Examples:
Input: player says "follow me" -> Output: {{"action":"continue_follow","target_x":null,"target_y":null,"target_id":null,"reason":"player explicitly asked to keep following"}}
Input: player says "go grab that axe", axe_01 is nearby -> Output: {{"action":"pick_up_item","target_x":null,"target_y":null,"target_id":"axe_01","reason":"player specified a clear pickup target"}}
Input: event report "strange noise heard nearby, at (120,340)", no player command -> Output: {{"action":"investigate","target_x":120,"target_y":340,"target_id":null,"reason":"a non-urgent but worth-checking event occurred"}}
Input: event report "threat sensed but not yet under attack" -> Output: {{"action":"retreat","target_x":null,"target_y":null,"target_id":null,"reason":"potential threat present, prioritize staying close to the player"}}
"""


# ---------------------------------------------------------------------------
# 4. User prompt (varies per request: player_command vs event_batch)
# ---------------------------------------------------------------------------
def build_user_prompt(request: dict) -> str:
    trigger_type = request.get("trigger_type")
    state = request.get("state", {})

    state_summary = (
        f"Player health: {state.get('health', 'unknown')}\n"
        f"Nearby zombies: {len(state.get('nearby_zombies', []))}\n"
        f"Nearby items: {state.get('nearby_items', [])}\n"
    )

    if trigger_type == "player_command":
        return (
            f"[Triggered by player command]\n"
            f"Player said: \"{request.get('player_command', '')}\"\n\n"
            f"Current state:\n{state_summary}\n"
            f"Choose one action."
        )

    elif trigger_type == "event_batch":
        events = request.get("events", [])
        event_lines = "\n".join(
            f"- {e.get('type')}: {e.get('detail', '')}" for e in events
        )
        return (
            f"[Triggered by an event report, no player command]\n"
            f"The following happened recently:\n{event_lines}\n\n"
            f"Current state:\n{state_summary}\n"
            f"Choose one action."
        )

    else:
        raise ValueError(f"Unknown trigger_type: {trigger_type}")


# ---------------------------------------------------------------------------
# 5. Call local llama.cpp server
# ---------------------------------------------------------------------------
def call_llama_cpp(system_prompt: str, user_prompt: str, timeout: float = 10.0) -> dict:
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "schema": ACTION_SCHEMA,
        },
        "temperature": 0,
        "stream": False,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLAMA_CPP_URL, data=data, headers={"Content-Type": "application/json"}
    )

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach llama.cpp server at {LLAMA_CPP_URL}. Make sure "
            f"`llama-server` is running and listening on port 8080. "
            f"Original error: {e}"
        )
    elapsed = time.time() - start

    raw_content = body["choices"][0]["message"]["content"]
    intent = json.loads(raw_content)

    return {"intent": intent, "elapsed_seconds": round(elapsed, 3)}


# ---------------------------------------------------------------------------
# 6. CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="PZ Companion local inference test script"
    )
    parser.add_argument(
        "--fixture",
        required=True,
        help="Path to a test state JSON file"
    )
    args = parser.parse_args()

    with open(args.fixture, "r", encoding="utf-8") as f:
        request = json.load(f)

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(request)

    print("=" * 60)
    print("USER PROMPT:")
    print(user_prompt)
    print("=" * 60)

    result = call_llama_cpp(system_prompt, user_prompt)

    print(f"Elapsed: {result['elapsed_seconds']}s")
    print("Model output:")
    print(json.dumps(result["intent"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()