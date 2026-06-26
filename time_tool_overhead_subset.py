#!/usr/bin/env python3
"""
time_tool_overhead_subset.py - isolates whether the tool-schema overhead
measured in time_tool_overhead.py scales with schema SIZE (number of tools /
description length) or is mostly a FIXED cost from Ollama's constrained
decoding grammar kicking in at all.

Run from repo root, inside the bot's venv:

    .venv/bin/python -m bot.time_tool_overhead_subset

It times four cases:
  1. No tools at all (baseline)
  2. Just 1 tool (add_novel) - minimal schema
  3. A 5-tool subset relevant to "add the novel Solo Leveling"
  4. The full 21-tool schema (same as time_tool_overhead.py)

If (2) is already close to (4), the overhead is mostly fixed/grammar-driven
and trimming descriptions won't help much - the fix is routing to fewer
tools per request (or switching models), not shrinking docstrings.

If (2) is close to (1) and the time climbs steadily from (2) -> (3) -> (4),
overhead scales with schema size - both trimming descriptions AND sending
fewer tools per request will help.
"""
import os
import sys
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_agent  # noqa: E402

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "phi4-mini")


def timed_call(label, tools=None):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": ai_agent.SYSTEM_INSTRUCTION},
            {"role": "user", "content": "add the novel Solo Leveling"},
        ],
        "stream": False,
        "options": ai_agent.OLLAMA_CHAT_OPTIONS,
        "keep_alive": ai_agent.OLLAMA_KEEP_ALIVE,
    }
    if tools:
        payload["tools"] = tools

    start = time.monotonic()
    resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=300)
    elapsed = time.monotonic() - start
    resp.raise_for_status()
    print(f"{label}: {elapsed:.1f}s")
    return elapsed


class _FakeBrain:
    def __getattr__(self, name):
        return lambda *a, **k: "(stub - not actually called)"


fake_brain = _FakeBrain()
all_py_tools = ai_agent._build_tools(fake_brain)
by_name = {fn.__name__: fn for fn in all_py_tools}

# Subset relevant to the test message ("add the novel Solo Leveling")
relevant_names = ["add_novel", "add_anime", "list_library", "find_items", "get_stats"]
subset_5 = [by_name[n] for n in relevant_names if n in by_name]
subset_1 = [by_name["add_novel"]]

schema_all = [ai_agent._function_to_ollama_schema(fn) for fn in all_py_tools]
schema_5 = [ai_agent._function_to_ollama_schema(fn) for fn in subset_5]
schema_1 = [ai_agent._function_to_ollama_schema(fn) for fn in subset_1]

print(f"Full schema: {len(schema_all)} tools")
print(f"Subset schema: {len(schema_5)} tools")
print(f"Single-tool schema: {len(schema_1)} tool\n")

t0 = timed_call("1. No tools (baseline)")
t1 = timed_call("2. 1 tool (add_novel only)", tools=schema_1)
t2 = timed_call("3. 5-tool relevant subset", tools=schema_5)
t3 = timed_call("4. Full 21-tool schema", tools=schema_all)

print("\n--- Summary ---")
print(f"Baseline (no tools):     {t0:.1f}s")
print(f"+1 tool overhead:        {t1 - t0:+.1f}s  (total {t1:.1f}s)")
print(f"+5 tool overhead:        {t2 - t0:+.1f}s  (total {t2:.1f}s)")
print(f"+21 tool overhead:       {t3 - t0:+.1f}s  (total {t3:.1f}s)")

# Quick interpretation hint
jump_to_one = t1 - t0
climb_after = t3 - t1
if jump_to_one > 0.6 * (t3 - t0):
    print(
        "\n=> Most of the overhead appears as soon as ANY tool is attached "
        "(fixed/grammar-driven cost). Trimming descriptions likely won't "
        "help much - focus on sending fewer tools per request or a "
        "different model/backend for tool calls."
    )
else:
    print(
        "\n=> Overhead climbs steadily with schema size/count. Both "
        "trimming tool descriptions AND routing to smaller tool subsets "
        "per request should meaningfully reduce latency."
    )
