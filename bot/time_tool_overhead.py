#!/usr/bin/env python3
"""
time_tool_overhead.py - measures exactly how much latency Ollama adds
when the bot's full 21-tool schema is attached vs a plain call with none,
and separately how long a real plain-chat message ("hey") takes with the
tightened no-tools options.

Run this ON THE SERVER, inside the bot's venv, from the repo root:

    .venv/bin/python bot/time_tool_overhead.py

It uses the bot's *real* _build_tools()/_function_to_ollama_schema() code
- not a guess at tool count/size - so the number it prints is the actual
overhead your /ask command is paying right now.
"""
import os
import sys
import time
import requests

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from bot import ai_agent  # noqa: E402

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "phi4-mini")


def timed_call(label, user_text, tools=None, use_tools_options=True):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": ai_agent.SYSTEM_INSTRUCTION},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": ai_agent._ollama_options(use_tools=use_tools_options),
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


# A no-op brain stub is enough - _build_tools() only needs it to build the
# closures; it's never actually called here, so none of its methods need
# to work.
class _FakeBrain:
    def __getattr__(self, name):
        return lambda *a, **k: "(stub - not actually called)"


fake_brain = _FakeBrain()
py_tools = ai_agent._build_tools(fake_brain)
ollama_tools = [ai_agent._function_to_ollama_schema(fn) for fn in py_tools]
print(f"Built {len(ollama_tools)} tool schemas from the bot's real tool list.\n")

no_tools_time = timed_call(
    "Without tools attached (tools-style msg)", "add the novel Solo Leveling",
    use_tools_options=False,
)
with_tools_time = timed_call(
    "With full tool schema attached", "add the novel Solo Leveling",
    tools=ollama_tools, use_tools_options=True,
)
hey_time = timed_call(
    "Real plain-chat message, no tools (this is what '/ask hey' actually pays)",
    "hey", use_tools_options=False,
)

print(f"\nTool-schema overhead: {with_tools_time - no_tools_time:.1f}s extra")
print(f"'/ask hey' end-to-end (post-fix settings): {hey_time:.1f}s")