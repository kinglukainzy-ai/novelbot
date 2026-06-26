"""
scraper.py - fetches a novel's page and extracts a "snapshot" (usually the
latest chapter title/number) so we can detect when it changes.

Tiers, cheapest/most-reliable first:
1. With a CSS selector  -> grabs the text of that element (most reliable,
   user supplies it once when adding the novel).
2. Without a selector    -> falls back to a heuristic: look for the first
   link/text containing the word "chapter" near the top of the page.
3. Local LLM (local_llm.py) -> page loaded fine but tiers 1-2 found nothing
   (unusual layout). Hands the already-fetched text to a local model - no
   internet access needed for this step, since the page is already in hand.
4. Gemini web search (fetch_snapshot_websearch) -> the page itself is
   unreachable, so there's no text for tier 3 to read. This is the one
   tier that genuinely needs to leave the box.
"""
import hashlib
import os
import time
import requests
from bs4 import BeautifulSoup

from bot import local_llm

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

TIMEOUT = 15


class ScrapeError(Exception):
    pass


class PageUnreachable(ScrapeError):
    """The page itself couldn't be loaded at all (down, geo-blocked, DNS,
    timeout...) - distinct from 'loaded fine but nothing matched', so
    callers can tell 'tier 3 never got a chance to look' apart from
    'tier 3 looked and found nothing'."""
    pass


def fetch_snapshot(url: str, selector: str | None = None) -> str:
    """
    Returns a short string representing 'the latest chapter as seen right now'.
    Raises ScrapeError if the page can't be fetched or the selector/heuristic
    finds nothing (this is how we detect a broken scraper).
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ScrapeError(f"Could not fetch page: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")

    if selector:
        el = soup.select_one(selector)
        if not el:
            raise ScrapeError(f"Selector '{selector}' matched nothing on the page")
        text = el.get_text(strip=True)
        if not text:
            raise ScrapeError(f"Selector '{selector}' matched an empty element")
        return text

    # Heuristic fallback: find links/text mentioning "chapter"
    candidates = []
    for tag in soup.find_all(["a", "li", "span", "div"], limit=500):
        text = tag.get_text(strip=True)
        if text and "chapter" in text.lower() and len(text) < 120:
            candidates.append(text)

    if not candidates:
        raise ScrapeError(
            "No selector given and couldn't auto-detect a 'chapter' element. "
            "Add this novel again with a CSS selector for best results."
        )

    # Most novel sites list the newest chapter first
    return candidates[0]


def snapshot_hash(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()[:16]


def parse_chapter_number(snapshot: str) -> int | None:
    """Pulls the first chapter number out of a snapshot string, e.g.
    'Chapter 551 (END) - Epilogue 5' -> 551. Used to populate
    last_chapter_num, which powers the cheap next-chapter probe shortcut.
    Returns None if no number is found - not every site's snapshot text
    contains one, and that's fine, the probe shortcut just won't apply."""
    import re
    m = re.search(r"chapter\s+(\d+)", snapshot, re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_chapter_number(snapshot: str) -> int | None:
    """Pulls the first integer following the word 'chapter' out of a
    snapshot string, e.g. 'Chapter 551 (END) - Epilogue 5' -> 551. Used to
    drive the tier-0 increment probe and numeric /update reporting. Returns
    None if no clean number is found - some sites' snapshot text never
    contains one (just a title), and that's fine, it just means tier 0
    doesn't apply to that item."""
    import re
    m = re.search(r"chapter\s+(\d+)", snapshot, re.IGNORECASE)
    return int(m.group(1)) if m else None


def fetch_page_text(url: str) -> str:
    """Re-fetches a page and returns its stripped visible text (script/style
    removed). Raises PageUnreachable - not the base ScrapeError - if the
    fetch itself fails, so callers can tell that apart from 'loaded fine,
    nothing useful in it'."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise PageUnreachable(f"Could not fetch page: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def fetch_snapshot_ai(url: str, title: str = "") -> str | None:
    """Tier 3: local LLM page-read. Re-fetches the page (raises
    PageUnreachable if that fails - tier 3 never got a chance to look) and
    hands the visible text to a local Ollama model to find the latest
    chapter marker.

    Returns None - never raises ScrapeError - if Ollama isn't configured/
    reachable, or the model genuinely finds nothing. PageUnreachable DOES
    propagate, since that's a different situation (no text to even try on)
    that callers should be able to tell apart from a clean 'not found'.
    """
    if not local_llm.is_configured():
        return None

    page_text = fetch_page_text(url)  # may raise PageUnreachable
    if not page_text:
        return None

    return local_llm.extract_chapter_marker(title or url, page_text)


def _gemini_search(title: str):
    """One Gemini web-search attempt. Returns the chapter text, or a
    two-tuple-like sentinel distinguishing 'Gemini answered NONE' (a real
    answer) from a raised exception (the call itself failed - busy/rate
    limited/timed out, not a real answer). Raises on any API/network error
    so the caller can apply retry/backoff and tell it apart from NONE."""
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    prompt = (
        f"Search the web right now for the latest chapter of the web novel "
        f"'{title}'. Reply with ONLY the chapter number and title "
        f"(e.g. 'Chapter 412: The Final Battle'). "
        f"If you genuinely cannot find it, reply with exactly: NONE"
    )

    result = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        ),
    )
    return (result.text or "").strip()


def _tavily_search(title: str) -> str | None:
    """Backup web-search path for when Gemini itself is unavailable, not a
    routine second opinion. Uses Tavily's free tier (1,000 searches/month)
    to fetch real results, then hands them to whatever's reachable for
    extraction (local LLM first, since it's free and unlimited; this is
    just text-in/text-out once we have real search results in hand)."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return None
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": f"latest chapter of the web novel {title}",
                "max_results": 5,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    snippets = " ".join(
        r.get("content", "") for r in data.get("results", []) if r.get("content")
    )
    if not snippets:
        return None

    if local_llm.is_configured():
        return local_llm.extract_chapter_marker(title, snippets)

    # No local model available either - fall back to a plain heuristic
    # scan of the snippets rather than giving up outright.
    import re
    m = re.search(r"chapter\s+\d+[^.]{0,80}", snippets, re.IGNORECASE)
    return m.group(0).strip() if m else None


def fetch_snapshot_websearch(title: str, retries: int = 2) -> str | None:
    """Tier 4 - the only tier that genuinely needs live internet access,
    since the page itself is unreachable here. Tries the self-hosted
    SearXNG instance first (no API key, no quota - see bot/websearch.py).
    Only falls back to the Gemini -> Tavily chain below if SEARXNG_URL was
    never configured, so existing setups that haven't migrated yet don't
    lose this tier outright. Returns None - never raises - if nothing
    pans out, so this can never be the reason a check cycle crashes.
    """
    from bot import websearch
    if websearch.is_configured():
        results = websearch.search(f"latest chapter of the web novel {title}")
        if results:
            snippets = " ".join(r["content"] for r in results if r.get("content"))
            if snippets:
                if local_llm.is_configured():
                    return local_llm.extract_chapter_marker(title, snippets)
                import re
                m = re.search(r"chapter\s+\d+[^.]{0,80}", snippets, re.IGNORECASE)
                return m.group(0).strip() if m else None
        return None  # SearXNG is up; a clean empty result is a real answer

    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        for attempt in range(retries + 1):
            try:
                out = _gemini_search(title)
                if not out or out.upper() == "NONE":
                    return None  # a real answer: genuinely not found
                return out[:200]
            except Exception:
                if attempt < retries:
                    time.sleep(2 ** attempt)  # 1s, 2s backoff
                    continue
                # Gemini itself never came through - fall through to the
                # backup search tier below rather than giving up here.
                break

    return _tavily_search(title)

