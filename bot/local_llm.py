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
    trimmed to visible text (script/style stripped) by the caller."""
    model = os.getenv("OLLAMA_MODEL", "phi4-mini")
    prompt = (
        f"Below is the visible text of the web novel '{title}'s page. "
        "Find the latest/newest chapter being shown (title and/or number). "
        "Reply with ONLY that chapter marker, nothing else - no explanation, "
        "no quotes. If you genuinely can't find one, reply with exactly: "
        "NONE\n\n" + page_text[:6000]
    )
    try:
        resp = requests.post(
            f"{_ollama_host()}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        out = (resp.json().get("response") or "").strip()
    except (requests.RequestException, ValueError):
        return None

    if not out or out.upper().startswith("NONE"):
        return None
    return out[:200]
