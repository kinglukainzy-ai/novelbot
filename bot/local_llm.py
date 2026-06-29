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

NOTE on OLLAMA_MODEL: if you've followed THOTH_MODELFILE_INTEGRATION.md
and set OLLAMA_MODEL=thoth (the custom model with Thoth's chat persona
baked in via the repo-root Modelfile), every function below runs against
"thoth" too, not just bot/ai_agent.py's /ask command. That's safe -
extract_chapter_marker() and answer_from_search_results() both send their
own explicit system message per call below, which overrides the baked
persona for that call - so chapter extraction stays a plain extraction
task and never picks up Thoth's chat voice. Just don't be surprised if
`ollama ps` shows "thoth" loaded during a scraper check cycle, not only
during /ask.
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

    Defenses against a small model "continuing the story" instead of
    reading it (this is a real, observed failure mode - phi4-mini given a
    single blob of text via /api/generate once returned a chapter number
    that simply wasn't in the source, having treated the prompt as
    something to continue rather than extract from):

    1. Ollama's `format` parameter with a JSON schema (supported since
       Ollama 0.3+) grammar-constrains the model's decoding to match that
       schema. This is the root fix for "rambling past the answer" - the
       model structurally cannot emit a code fence, a restated question,
       or freeform prose, because the sampler itself is constrained to
       valid JSON matching the shape below. This replaces the old
       approach of asking for plain text and then slicing the first line,
       which only patched the symptom.
    2. Temperature 0, for determinism on top of the schema constraint.
    3. One retry on a parse failure (the schema makes malformed JSON rare,
       but Ollama doesn't validate that generation didn't stop mid-object,
       so a corrupt response is still possible in principle).
    4. Grounding check: whatever chapter number comes back MUST actually
       appear in the source text, or the result is discarded as a failed
       extraction (None) rather than trusted. This makes a hallucinated
       number structurally impossible to act on, regardless of how the
       model misbehaves - kept as defense-in-depth even with the schema
       constraint above, since the schema guarantees valid JSON shape,
       not factual grounding.
    """
    import json
    import re

    model = os.getenv("OLLAMA_MODEL", "phi4-mini")
    system_msg = (
        "You extract information from text. You never invent, continue, or "
        "guess - you only repeat back what is literally present in the text "
        "you are given."
    )
    user_msg = (
        f"Here is the visible text of the '{title}' book page:\n\n"
        f"---\n{page_text[:6000]}\n---\n\n"
        "Find the latest/newest chapter mentioned in that text above "
        "(its title and/or number), copied exactly as it appears. If no "
        "chapter is mentioned anywhere in the text, set chapter_marker to "
        "null."
    )
    schema = {
        "type": "object",
        "properties": {
            "chapter_marker": {"type": ["string", "null"]},
        },
        "required": ["chapter_marker"],
    }

    out = None
    for attempt in range(2):  # one retry on a malformed/incomplete response
        try:
            resp = requests.post(
                f"{_ollama_host()}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    "format": schema,
                    "options": {"temperature": 0, "num_predict": 60},
                    "stream": False,
                },
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            raw = (resp.json().get("message", {}).get("content") or "").strip()
            parsed = json.loads(raw)
            out = parsed.get("chapter_marker")
            break  # got valid JSON - no need to retry even if out is None
        except (requests.RequestException, ValueError, KeyError, AttributeError):
            if attempt == 0:
                continue  # one retry
            return None

    if not out or not isinstance(out, str):
        return None
    out = out.strip()[:200]
    if not out:
        return None

    # Grounding check: a chapter number in the answer must actually be
    # present in the source text, or this is a hallucination, not an
    # extraction - discard it rather than risk acting on a wrong number.
    answer_num = re.search(r"\d+", out)
    if answer_num and answer_num.group(0) not in page_text:
        return None

    return out


def answer_from_search_results(query: str, snippet_block: str) -> str | None:
    """Turns raw SearXNG result snippets into a direct answer to the
    original query - the local-model equivalent of what Gemini's
    google_search tool used to do in one call. Same text-in/text-out shape
    as extract_chapter_marker: no internet access needed here, since the
    actual web search already happened upstream in websearch.py.

    Returns None - never raises - on any failure, so callers can fall back
    to handing back the raw snippets instead."""
    model = os.getenv("OLLAMA_MODEL", "phi4-mini")
    system_msg = (
        "You answer questions using ONLY the search result snippets you "
        "are given. You never invent facts beyond what's in the snippets. "
        "If the snippets don't actually answer the question, say so plainly."
    )
    user_msg = (
        f"Question: {query}\n\n"
        f"Search result snippets:\n---\n{snippet_block[:6000]}\n---\n\n"
        "Answer the question in 2-4 sentences using only the information "
        "above. Mention which source it came from if relevant."
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
                "options": {"temperature": 0.3, "num_predict": 250},
                "stream": False,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        out = (resp.json().get("message", {}).get("content") or "").strip()
    except (requests.RequestException, ValueError, KeyError):
        return None

    return out or None
