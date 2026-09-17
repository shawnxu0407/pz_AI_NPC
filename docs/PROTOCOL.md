# AI Companion Protocol v1

## Overview

Two-file JSON protocol between Project Zomboid Lua mod and a local Python sidecar.
No sockets, no HTTP between PZ and sidecar. The sidecar talks to a local LLM
(llama.cpp / vLLM) over HTTP, but PZ only sees files on disk.

## File layout

All files live under `<PZ user dir>/Lua/AIAgent/`:

```
AIAgent/
  ai_request.json   # Lua writes, sidecar reads
  ai_response.json  # sidecar writes, Lua reads
```

## Request (Lua -> sidecar)

Written by `AIAgent.lua` when the player sends a message.

```json
{
  "protocol_version": 1,
  "request_id": "req-1",
  "npc_id": "12345",
  "event": "PLAYER_MESSAGE",
  "player_message": "follow me",
  "current_mode": "STANDBY",
  "npc_health": 0.85,
  "npc_distance": 4.2,
  "timestamp": 1234567890
}
```

| Field | Type | Description |
| --- | --- | --- |
| `protocol_version` | int | Always `1` |
| `request_id` | string | Unique per request, used for dedup |
| `npc_id` | string | Bandits zombie ID of the companion |
| `event` | string | Event type (Phase 1: only `PLAYER_MESSAGE`) |
| `player_message` | string | What the player typed |
| `current_mode` | string | Current high-level mode: `FOLLOW` or `STANDBY` |
| `npc_health` | float | NPC health 0.0-1.0 |
| `npc_distance` | float | Distance from NPC to player |
| `timestamp` | int | Epoch milliseconds |

## Response (sidecar -> Lua)

Written by `sidecar.py` after LLM inference.

```json
{
  "protocol_version": 1,
  "request_id": "req-1",
  "npc_id": "12345",
  "action": "FOLLOW"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `protocol_version` | int | Always `1` |
| `request_id` | string | Must match the request |
| `npc_id` | string | Must match the request |
| `action` | string | One of: `FOLLOW`, `STANDBY` |

## Valid actions (Phase 1)

| Action | Description | Bandits mapping |
| --- | --- | --- |
| `FOLLOW` | Follow the player | `GetMoveTaskTarget` toward master, loop |
| `STANDBY` | Stop and wait | `ForceStationary(true)` + `Idle` animations |

## Lua-side validation

The Lua mod validates every response before acting:

1. JSON parse succeeds
2. `protocol_version` == 1
3. `request_id` not already processed (dedup)
4. `npc_id` matches active NPC
5. `action` is in `{FOLLOW, STANDBY}`

Invalid responses are logged and discarded. The game never crashes.

## Stale / missing / duplicate responses

- **Timeout**: If no response arrives within 10s, Lua stops polling for that `request_id`.
- **Stale**: If a response arrives for a `request_id` already processed, it is ignored.
- **Duplicate**: Tracked via `AIAgent.LastRequestId[npcId]`.
- **NPC gone**: If the NPC dies or despawns, `AIAgent.ActiveNpcId` is cleared.
- **Malformed**: Parse errors are caught with `pcall` and logged.

## Flow

```
Player presses F8 -> types "come with me"
  |
  v
Lua writes ai_request.json
  |
  v
Sidecar polls (500ms) -> reads request -> calls LLM
  |
  v
LLM returns {"action": "FOLLOW"}
  |
  v
Sidecar validates -> writes ai_response.json
  |
  v
Lua polls (500ms) -> reads response -> validates -> stores PendingCommand
  |
  v
Next AICompanion.Main tick:
  PendingCommand -> CurrentMode = FOLLOW
  ClearTasks(bandit)
  |
  v
Subsequent ticks:
  CurrentMode == FOLLOW
  -> GetMoveTaskTarget(master.x, master.y, master.z)
  -> brain.tasks queue
  -> ProcessTask -> ZombieActions.Move -> PZ pathfinding
  |
  v
Combat interrupts (ManageCombat priority > Program)
  -> After combat ends, CurrentMode still FOLLOW -> resumes following
```
