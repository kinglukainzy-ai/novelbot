"""
local_llm.py - tier 3 of the scraper fallback pipeline.

When a novel's CSS selector AND the "chapter" heuristic both come up empty
(site redesign, unusual layout), the page text has already been fetched -
all that's missing is something to *read* it and point out the latest
chapter marker. That's a pure text-in/text-out job, so it doesn't need
internet access of its own and is a good fit for a small model running
locally via Ollama (https://ollama.com) - no API key, no rate limit, no
per-call cost, and it never leaves the box.

Only tier 4 (fetch_snapshot_websearch in scraper.py) genuinely needs to
reach the internet - that's the one case a local model can't help with on
its own, since the page itself is unreachable there.

Returns None - never raises - on any failure (Ollama not running, model
not pulled, bad response, etc). Callers treat that exactly like any other
failed tier and fall through to the next one.
"""
import os
import requests

TIMEOUT = 60  # local CPU inference can take a while; this is a background job


def _ollama_host():
    return os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def is_configured() -> bool:
    """Whether a local Ollama host is actually reachable right now. Cheap
    check - callers can use this to skip straight to tier 4 instead of
    waiting out a connect-timeout on every single check cycle."""
    try:
        resp = requests.get(f"{_ollama_host()}/api/tags", timeout=3)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def extract_chapter_marker(title: str, page_text: str) -> str | None:
    """Hands already-fetched page text to a local model and asks it to
    point out the latest chapter marker. page_text should already be
    trimmed to visible text (script/style stripped) by the caller.

    Two defenses against a small model "continuing the story" instead of
    reading it (this is a real, observed failure mode - phi4-mini given a
    single blob of text via /api/generate once returned a chapter number
    that simply wasn't in the source, having treated the prompt as
    something to continue rather than extract from):

    1. Use /api/chat with a separate system instruction + temperature 0,
       which instruct models follow far more reliably than one continuous
       prompt that reads like the start of a story.
    2. Grounding check: whatever chapter number comes back MUST actually
       appear in the source text, or the result is discarded as a failed
       extraction (None) rather than trusted. This makes a hallucinated
       number structurally impossible to act on, regardless of how the
       model misbehaves.
    """
    import re

    model = os.getenv("OLLAMA_MODEL", "phi4-mini")
    system_msg = (
        "You extract information from text. You never invent, continue, or "
        "guess - you only repeat back what is literally present in the text "
        "you are given. If the requested information is not present, you "
        "say so exactly as instructed."
    )
    user_msg = (
        f"Here is the visible text of the '{title}' book page:\n\n"
        f"---\n{page_text[:6000]}\n---\n\n"
        "Find the latest/newest chapter mentioned in that text above "
        "(its title and/or number). Reply with ONLY that chapter marker, "
        "copied exactly as it appears - no explanation, no extra words. "
        "If no chapter is mentioned in the text, reply with exactly: NONE"
    )
    try:
        resp = requests.post(
            f"{_ollama_host()}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "options": {"temperature": 0},
                "stream": False,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        out = (resp.json().get("message", {}).get("content") or "").strip()
    except (requests.RequestException, ValueError, KeyError):
        return None

    if not out or out.upper().startswith("NONE"):
        return None
    out = out[:200]

    # Grounding check: a chapter number in the answer must actually be
    # present in the source text, or this is a hallucination, not an
    # extraction - discard it rather than risk acting on a wrong number.
    answer_num = re.search(r"\d+", out)
    if answer_num and answer_num.group(0) not in page_text:
        return None

    return out