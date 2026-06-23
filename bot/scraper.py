"""
scraper.py - fetches a novel's page and extracts a "snapshot" (usually the
latest chapter title/number) so we can detect when it changes.

Two modes:
1. With a CSS selector  -> grabs the text of that element (most reliable,
   user supplies it once when adding the novel).
2. Without a selector    -> falls back to a heuristic: look for the first
   link/text containing the word "chapter" near the top of the page. This
   is best-effort and more likely to break on unusual site layouts.
"""
import hashlib
import os
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

TIMEOUT = 15


class ScrapeError(Exception):
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


def fetch_snapshot_ai(url: str) -> str | None:
    """Last-resort fallback for when fetch_snapshot() has already failed with
    both a selector and the 'chapter' heuristic (e.g. a site redesign, or a
    layout the heuristic just doesn't understand). Re-fetches the page and
    hands its visible text to Gemini, asking it to point out the latest
    chapter marker.

    Returns None - never raises - if GEMINI_API_KEY isn't set, the page
    can't be fetched, or Gemini can't find anything. This is intentionally
    best-effort: callers already have their own broken/healthy bookkeeping
    and should treat None exactly like any other failed scrape attempt.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    # Strip script/style noise before handing text to the model.
    for tag in soup(["script", "style"]):
        tag.decompose()
    page_text = soup.get_text(separator=" ", strip=True)[:6000]
    if not page_text:
        return None

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        prompt = (
            "Below is the visible text of a web novel's page. Find the "
            "latest/newest chapter being shown (title and/or number). "
            "Reply with ONLY that chapter marker, nothing else - no "
            "explanation, no quotes. If you genuinely can't find one, "
            "reply with exactly: NONE\n\n" + page_text
        )
        result = client.models.generate_content(model=model, contents=prompt)
        out = (result.text or "").strip()
    except Exception:
        return None

    if not out or out.upper() == "NONE":
        return None
    return out[:200]
