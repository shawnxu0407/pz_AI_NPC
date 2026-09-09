# Databricks notebook source
# DBTITLE 1,Setup — Import pipeline and load fixtures
import sys, json
from pathlib import Path

# Add the pipeline package to path (works both in Databricks and locally)
pipeline_dir = Path.cwd().parent if Path.cwd().name == "llm_pipeline" else Path.cwd()
if str(pipeline_dir) not in sys.path:
    sys.path.insert(0, str(pipeline_dir))

from llm_pipeline import CompanionPipeline, PromptEngine, init_client, validate_intent
from llm_pipeline.config import ACTION_SET, ACTION_NAMES, COMPANION_PROFILES

# Load test scenarios
fixtures_path = Path("fixtures/test_scenarios.json")
if not fixtures_path.exists():
    # Try relative to notebook location in Databricks
    fixtures_path = Path("/Workspace/Users/shawn.xu@tempered.ai/pz_AI_NPC/runtime/llm_pipeline/fixtures/test_scenarios.json")

with open(fixtures_path, "r") as f:
    scenarios = json.load(f)["scenarios"]

print(f"Loaded {len(scenarios)} test scenarios:")
for s in scenarios:
    print(f"  - {s['name']}: {s['description']} (expected: {s['expected_action']})")

# COMMAND ----------

# DBTITLE 1,Prompt Preview — Inspect generated prompts without calling LLM
# Preview prompts for any scenario without hitting the LLM
# Useful for iterating on prompt wording

SCENARIO_IDX = 4  # Change this to preview different scenarios
PROFILE = "cautious_survivor"  # Try: neutral, cautious_survivor, aggressive_fighter, resourceful_scavenger

engine = PromptEngine(profile_key=PROFILE)
scenario = scenarios[SCENARIO_IDX]

print(f"=== Scenario: {scenario['name']} ===")
print(f"=== Profile: {PROFILE} ({COMPANION_PROFILES[PROFILE]['name']}) ===")
print()
print("--- SYSTEM PROMPT ---")
print(engine.build_system_prompt())
print()
print("--- USER PROMPT ---")
print(engine.build_user_prompt(scenario["request"]))
print()
print(f"Expected action: {scenario['expected_action']}")

# COMMAND ----------

# DBTITLE 1,Offline Validation Test — Test intent validator without LLM
# Test the validation layer with simulated LLM outputs
# Ensures safety guarantees work before connecting to actual model

test_outputs = [
    # Valid outputs
    {"action": "continue_follow", "target_x": None, "target_y": None, "target_id": None, "reason": "player said follow"},
    {"action": "investigate", "target_x": 120, "target_y": 340, "target_id": None, "reason": "noise at location"},
    {"action": "pick_up_item", "target_x": None, "target_y": None, "target_id": "axe_01", "reason": "player asked for axe"},
    # Edge cases: should trigger warnings
    {"action": "wait", "target_x": 100, "target_y": 200, "target_id": None, "reason": "waiting"},  # unnecessary coords
    {"action": "investigate", "target_x": None, "target_y": None, "target_id": None, "reason": "noise"},  # missing required coords
    {"action": "pick_up_item", "target_x": None, "target_y": None, "target_id": None, "reason": "grab it"},  # missing target_id
    # Invalid outputs: should fallback safely
    {"action": "fly_away", "target_x": None, "target_y": None, "target_id": None, "reason": "escape"},  # invalid action
    None,  # parse failure
]

print(f"{'Output':<50} {'Valid':<6} {'Action':<18} {'Intent':<12} {'Conf':<5} {'Warnings'}")
print("-" * 120)
for output in test_outputs:
    result = validate_intent(output, raw_content=str(output))
    label = str(output)[:48] if output else "None (parse failure)"
    warnings = "; ".join(result.warnings) if result.warnings else "-"
    print(f"{label:<50} {str(result.valid):<6} {result.action:<18} {result.intent:<12} {result.confidence:<5} {warnings}")

# COMMAND ----------

# DBTITLE 1,LLM Pipeline — Initialize and run single scenario
# === CONFIGURE YOUR BACKEND HERE ===
# Uncomment the one matching your local setup:

# llama.cpp (default)
pipe = CompanionPipeline(profile="neutral")

# vLLM
# pipe = CompanionPipeline(profile="neutral", base_url="http://localhost:8000/v1", model="Qwen/Qwen2.5-0.5B-Instruct")

# Ollama
# pipe = CompanionPipeline(profile="neutral", base_url="http://localhost:11434/v1", model="qwen2.5:0.5b")

# --- Run single scenario ---
SCENARIO_IDX = 0
scenario = scenarios[SCENARIO_IDX]

print(f"Running: {scenario['name']} (expected: {scenario['expected_action']})")
print()

result = pipe.decide(scenario["request"])
summary = result.summary()

print(f"Action:     {summary['action']}")
print(f"Intent:     {summary['intent']}")
print(f"Valid:      {summary['valid']}")
print(f"Confidence: {summary['confidence']}")
print(f"Reason:     {summary['reason']}")
print(f"Elapsed:    {summary['elapsed_ms']}ms")
print(f"Tokens:     {summary['tokens']}")
if summary['warnings']:
    print(f"Warnings:   {summary['warnings']}")
if summary['error']:
    print(f"ERROR:      {summary['error']}")
print()
match = '✓ PASS' if summary['action'] == scenario['expected_action'] else '✗ FAIL'
print(f"Match: {match}")

# COMMAND ----------

# DBTITLE 1,Batch Evaluation — Run all scenarios and compare results
# Run all test scenarios and compare against expected actions
pipe = CompanionPipeline(profile="neutral")

requests = [s["request"] for s in scenarios]
labels = [s["name"] for s in scenarios]
expected = [s["expected_action"] for s in scenarios]

results = pipe.batch_decide(requests, labels=labels)

# Results table
print(f"{'Scenario':<30} {'Expected':<20} {'Got':<20} {'Match':<6} {'Ms':<6} {'Warnings'}")
print("=" * 110)

passed = 0
for r, exp in zip(results, expected):
    match = "✓" if r["action"] == exp else "✗"
    if r["action"] == exp:
        passed += 1
    warnings = "; ".join(r["warnings"]) if r["warnings"] else "-"
    print(f"{r['label']:<30} {exp:<20} {r['action']:<20} {match:<6} {r['elapsed_ms']:<6} {warnings}")

print()
print(f"Pass rate: {passed}/{len(scenarios)} ({100*passed/len(scenarios):.0f}%)")
print(f"Total time: {sum(r['elapsed_ms'] for r in results)}ms")
print(f"Avg latency: {sum(r['elapsed_ms'] for r in results) / len(results):.0f}ms")

# COMMAND ----------

# DBTITLE 1,Profile Comparison — Same scenario, different companion personalities
# Compare how different personalities react to the same threat scenario
# This is the key experiment for tuning companion behavior

THREAT_SCENARIO = scenarios[4]  # event_single_zombie_close
print(f"Scenario: {THREAT_SCENARIO['name']} - {THREAT_SCENARIO['description']}")
print()

for profile_key, profile in COMPANION_PROFILES.items():
    pipe = CompanionPipeline(profile=profile_key)
    result = pipe.decide(THREAT_SCENARIO["request"])
    s = result.summary()
    print(f"  [{profile_key:>25}] {profile['name']:<12} -> {s['action']:<18} reason: {s['reason']}")

# COMMAND ----------

# DBTITLE 1,Diagnostics — Token usage and latency analysis
# Analyze token usage and latency from the batch run
# Critical for ensuring the pipeline fits within game-loop timing budgets

from llm_pipeline.llm_backend import get_client

stats = get_client().stats
print("=== LLM Client Stats ===")
print(f"Total calls:  {stats['calls']}")
print(f"Total time:   {stats['total_ms']}ms")
print(f"Avg latency:  {stats['avg_ms']}ms")
print(f"Errors:       {stats['errors']}")
print()

# Per-run breakdown from the last batch
if pipe.run_log:
    latencies = [r['elapsed_ms'] for r in pipe.run_log]
    print(f"Run log ({len(pipe.run_log)} runs):")
    print(f"  Min latency:  {min(latencies)}ms")
    print(f"  Max latency:  {max(latencies)}ms")
    print(f"  Avg latency:  {sum(latencies)/len(latencies):.0f}ms")
    print()
    print("Budget check (target: <500ms for game responsiveness):")
    over_budget = [r for r in pipe.run_log if r['elapsed_ms'] > 500]
    if over_budget:
        print(f"  ⚠ {len(over_budget)} runs exceeded 500ms budget")
        for r in over_budget:
            print(f"    - {r.get('label', '?')}: {r['elapsed_ms']}ms")
    else:
        print("  ✓ All runs within 500ms budget")

# COMMAND ----------

# DBTITLE 1,Sidecar Integration Example — How to plug into whg_companion_sidecar.py
# MAGIC %md
# MAGIC ## Sidecar Integration
# MAGIC
# MAGIC To replace `deterministic_reply()` in `runtime/spike002/whg_companion_sidecar.py`:
# MAGIC
# MAGIC ```python
# MAGIC # In whg_companion_sidecar.py, add to imports:
# MAGIC from llm_pipeline import CompanionPipeline
# MAGIC
# MAGIC # Initialize once at startup:
# MAGIC pipeline = CompanionPipeline(
# MAGIC     profile="neutral",
# MAGIC     base_url="http://localhost:8080/v1",
# MAGIC )
# MAGIC
# MAGIC # Replace deterministic_reply() call in make_success_response():
# MAGIC def make_success_response(request, processing_ms):
# MAGIC     # Convert protocol v1 request to pipeline format
# MAGIC     pipeline_request = {
# MAGIC         "trigger_type": "player_command",
# MAGIC         "player_command": request["playerText"],
# MAGIC         "state": request.get("context", {}),
# MAGIC     }
# MAGIC     
# MAGIC     result = pipeline.decide(pipeline_request)
# MAGIC     return result.intent_result.to_protocol_response(
# MAGIC         request_id=request["requestId"],
# MAGIC         processing_ms=result.elapsed_ms,
# MAGIC     )
# MAGIC ```
# MAGIC
# MAGIC The `IntentResult.to_protocol_response()` method outputs the exact JSON schema that `IPC_PROTOCOL_V1.md` requires.