#!/usr/bin/env python3
"""sidecar.py — Phase 1 minimal LLM sidecar for PZ AI Companion.

Polls for ai_request.json written by the Lua mod, sends the player's
message + game state to a local llama.cpp (or any OpenAI-compatible)
endpoint, validates the response, and writes ai_response.json.

The LLM is constrained to output ONLY one of: "FOLLOW", "STANDBY".
If the LLM output is unparseable or invalid, the sidecar falls back
to a simple keyword match so the mod never hangs.

Usage:
    python sidecar.py [--ipc-dir ~/Zomboid/Lua/AIAgent] [--base-url http://localhost:8080/v1] [--model qwen2.5-1.5b-instruct]

Protocol files (inside --ipc-dir):
    ai_request.json   — written by Lua, read by sidecar
    ai_response.json   — written by sidecar, read by Lua
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# -- OpenAI-compatible HTTP client via urllib (no pip deps) ----
import urllib.request
import urllib.error

LOG = logging.getLogger("pz-sidecar")

# ========================================================================
# Config
# ========================================================================

PROTOCOL_VERSION = 1
VALID_ACTIONS = {"FOLLOW", "STANDBY"}
DEFAULT_BASE_URL = "http://localhost:8080/v1"
DEFAULT_MODEL = "qwen2.5-1.5b-instruct"
POLL_INTERVAL_S = 0.5
LLM_TIMEOUT_S = 10.0

SYSTEM_PROMPT = """You are a companion NPC in Project Zomboid. The player has spoken to you.
Based on the player's message, choose exactly ONE action.

Allowed actions:
- FOLLOW  — Follow the player, stay close
- STANDBY — Stop moving and wait in place

Respond with ONLY a JSON object, nothing else:
  {"action": "FOLLOW"}
  {"action": "STANDBY"}

Do not include any other text, explanation, or fields."""

# ========================================================================
# File I/O
# ========================================================================

def atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically using temp file + os.replace."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def read_json(path: Path) -> dict | None:
    """Read JSON from file, return None if missing or invalid."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

# ========================================================================
# LLM call (OpenAI-compatible chat completions)
# ========================================================================

def call_llm(base_url: str, model: str, messages: list[dict], timeout: float) -> dict:
    """Call OpenAI-compatible /chat/completions endpoint. Returns parsed JSON."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 64,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        LOG.error("LLM call failed: %s", exc)
        return {"content": ""}
    content = ""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        pass
    return {"content": content}

# ========================================================================
# Response validation & extraction
# ========================================================================

ACTION_RE = re.compile(r\'"action"\s*:\s*"([A-Z]+)"\', re.IGNORECASE)

def extract_action(raw: str) -> str | None:
    """Try JSON parse first, then regex fallback."""
    try:
        obj = json.loads(raw)
        a = obj.get("action", "").upper()
        if a in VALID_ACTIONS:
            return a
    except json.JSONDecodeError:
        pass
    m = ACTION_RE.search(raw)
    if m and m.group(1).upper() in VALID_ACTIONS:
        return m.group(1).upper()
    return None

def keyword_fallback(text: str) -> str:
    """Deterministic fallback when LLM is unavailable or unparseable."""
    t = text.lower()
    if any(w in t for w in ["wait", "stay", "stop", "hold", "stand"]):
        return "STANDBY"
    if any(w in t for w in ["follow", "come", "go", "move", "walk", "run", "with"]):
        return "FOLLOW"
    return "FOLLOW"  # default safe action

def build_response(request: dict, action: str) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(request.get("request_id", "")),
        "npc_id": str(request.get("npc_id", "")),
        "action": action,
    }

# ========================================================================
# Main loop
# ========================================================================

def run(ipc_dir: Path, base_url: str, model: str) -> None:
    req_file = ipc_dir / "ai_request.json"
    resp_file = ipc_dir / "ai_response.json"
    ipc_dir.mkdir(parents=True, exist_ok=True)
    processed_ids: set[str] = set()

    LOG.info("Sidecar started. IPC dir: %s", ipc_dir)
    LOG.info("LLM endpoint: %s  model: %s", base_url, model)

    while True:
        time.sleep(POLL_INTERVAL_S)

        req = read_json(req_file)
        if not req:
            continue

        req_id = str(req.get("request_id", ""))
        if not req_id or req_id in processed_ids:
            continue

        LOG.info("Processing request %s", req_id)

        player_msg = str(req.get("player_message", ""))
        if not player_msg:
            LOG.warning("Empty player_message, skipping")
            processed_ids.add(req_id)
            continue

        # Build LLM prompt
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f\'Player says: "{player_msg}"\\n\'
                    f\'Current mode: {req.get("current_mode", "STANDBY")}\\n\'
                    f\'NPC health: {req.get("npc_health", 1.0)}\\n\'
                    f\'Distance to player: {req.get("npc_distance", 0)}\\n\'
                    f\'\\nRespond with ONLY: {{"action": "FOLLOW"}} or {{"action": "STANDBY"}}\'
                ),
            },
        ]

        # Call LLM
        result = call_llm(base_url, model, messages, LLM_TIMEOUT_S)
        action = extract_action(result.get("content", ""))

        if action is None:
            LOG.warning("LLM output unparseable: %r, using keyword fallback", result.get("content"))
            action = keyword_fallback(player_msg)

        response = build_response(req, action)
        atomic_write_json(resp_file, response)
        processed_ids.add(req_id)
        LOG.info("Response written: action=%s req=%s", action, req_id)

        # Trim processed set to prevent unbounded growth
        if len(processed_ids) > 1000:
            processed_ids = set(list(processed_ids)[-500:])

def main() -> None:
    parser = argparse.ArgumentParser(description="PZ AI Companion sidecar")
    parser.add_argument(
        "--ipc-dir",
        default=str(Path.home() / "Zomboid" / "Lua" / "AIAgent"),
        help="IPC directory (default: ~/Zomboid/Lua/AIAgent)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"LLM API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        run(Path(args.ipc_dir), args.base_url, args.model)
    except KeyboardInterrupt:
        LOG.info("Sidecar stopped.")
        sys.exit(0)

if __name__ == "__main__":
    main()
