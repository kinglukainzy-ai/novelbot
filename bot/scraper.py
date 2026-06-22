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
