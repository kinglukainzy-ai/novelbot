"""
websearch.py - self-hosted, API-key-free web search via SearXNG.

Why this exists: the bot's only remaining outside dependency was Gemini's
google_search tool (for /ask's web_lookup escape hatch and for the
scraper's tier-4 "find it by web search" fallback). That meant the bot's
ability to answer live-info questions was capped by a free-tier quota that
can run out before it resets, and tied to Google specifically.

SearXNG (https://docs.searxng.org) is a free/open-source meta-search
engine you run yourself (one Docker container, see docker-compose.searxng.yml
in the repo root). It has no API key, no quota, no billing, and it queries
multiple backends in aggregate rather than hitting Google's API directly.
Once SEARXNG_URL is set, this becomes the default search path everywhere
in the bot; Gemini/Tavily become legacy fallbacks only used if you never
set SearXNG up.

Returns None - never raises - on any failure (container not running,
bad response, no results), so callers can fall through to the next tier
exactly like every other fallback in this codebase.
"""
import os
import requests

TIMEOUT = 15


def _searxng_url():
    url = os.getenv("SEARXNG_URL", "").strip()
    return url.rstrip("/") if url else ""


def is_configured() -> bool:
    """Whether a SEARXNG_URL is set AND the instance is actually reachable
    right now. Cheap check so callers can skip straight past this tier
    instead of waiting out a connect-timeout."""
    base = _searxng_url()
    if not base:
        return False
    try:
        resp = requests.get(f"{base}/search", params={"q": "ping", "format": "json"}, timeout=5)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def search(query: str, max_results: int = 5) -> list[dict] | None:
    """Runs a query against the self-hosted SearXNG instance and returns
    up to max_results results as [{"title", "url", "content"}, ...].
    Returns None if SEARXNG_URL isn't set or the request fails outright;
    returns [] (not None) if the search ran fine but found nothing, so
    callers can tell "search is broken" apart from "no results"."""
    base = _searxng_url()
    if not base:
        return None
    try:
        resp = requests.get(
            f"{base}/search",
            params={"q": query, "format": "json"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    results = []
    for r in data.get("results", [])[:max_results]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
        })
    return results


def search_and_summarize(query: str, max_results: int = 5) -> str | None:
    """Convenience wrapper: runs the search, then hands the raw snippets to
    the local Ollama model (if configured) to turn into a direct answer.
    Falls back to a plain joined-snippets string if no local model is
    available, so this still works on a bare SearXNG-only setup. Returns
    None only if the search itself found nothing or failed."""
    results = search(query, max_results=max_results)
    if results is None:
        return None
    if not results:
        return "(no results found)"

    snippet_block = "\n\n".join(
        f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content']}"
        for r in results if r["content"]
    )
    if not snippet_block:
        return "(no results found)"

    from bot import local_llm
    if local_llm.is_configured():
        answer = local_llm.answer_from_search_results(query, snippet_block)
        if answer:
            return answer

    # No local model, or it failed to produce anything - fall back to
    # handing back the raw snippets rather than failing outright.
    return snippet_block[:1500]
