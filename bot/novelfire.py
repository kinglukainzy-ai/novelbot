"""
novelfire.py - search novelfire.net for novels and extract the latest chapter.

Used by brain.py's /add novel <title> (name-only mode) to auto-find a novel
on NovelFire without the user needing to supply a URL or CSS selector.
"""
import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

BASE_URL = "https://novelfire.net"
SEARCH_URL = f"{BASE_URL}/search"
TIMEOUT = 15


def search_novel(title: str, limit: int = 5) -> list[dict]:
    """Search NovelFire for novels matching *title*.

    Returns a list of dicts: [{"title": ..., "url": ...}, ...]
    The first result is the best match.  Returns an empty list if the
    search fails or finds nothing.
    """
    try:
        resp = requests.get(
            SEARCH_URL,
            params={"keyword": title},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    # NovelFire search results are typically in list items with novel info.
    # Try multiple selectors to handle site layout variations.
    novel_items = (
        soup.select(".novel-item")
        or soup.select(".book-item")
        or soup.select(".search-item")
        or soup.select("[class*='novel']")
    )

    for item in novel_items[:limit]:
        link = item.select_one("a[href]")
        if not link:
            continue
        href = link.get("href", "")
        if not href:
            continue
        # Normalise relative URLs
        if href.startswith("/"):
            href = BASE_URL + href

        # Only accept links that look like novel pages
        if "/book/" not in href and "/novel/" not in href:
            continue

        # Extract the title - prefer the title attribute, then visible text
        name = (
            link.get("title")
            or item.select_one("h3, h4, .novel-title, .title")
            and item.select_one("h3, h4, .novel-title, .title").get_text(strip=True)
            or link.get_text(strip=True)
        )
        if not name:
            continue

        results.append({"title": name, "url": href})

    # Fallback: if the structured selectors found nothing, scan all links
    # whose href contains /book/ — this is less precise but covers layout
    # changes.
    if not results:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/book/" not in href:
                continue
            if href.startswith("/"):
                href = BASE_URL + href
            name = a.get("title") or a.get_text(strip=True)
            if not name or len(name) < 3:
                continue
            # Deduplicate by URL
            if any(r["url"] == href for r in results):
                continue
            results.append({"title": name, "url": href})
            if len(results) >= limit:
                break

    return results


def latest_chapter_snapshot(url: str) -> str | None:
    """Fetch the latest chapter text from a NovelFire novel page.

    Returns a short string like "Chapter 123: The Final Battle" or None
    if extraction fails.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy 1: look for a dedicated "latest chapter" element.
    # NovelFire typically shows the latest chapter in an element with
    # class or id containing "latest" or "newest".
    for selector in (
        ".latest-chapter a",
        ".newest-chapter a",
        "[class*='latest'] a",
        ".chapter-latest a",
        ".last-chapter a",
        ".new-chapter a",
    ):
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text and "chapter" in text.lower():
                return text

    # Strategy 2: find the first chapter list item (newest-first ordering).
    for selector in (
        ".chapter-list li a",
        ".chapter-list a",
        ".list-chapter li a",
        "#chapter-list a",
        "ul.chapter-list a",
    ):
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) < 150:
                return text

    # Strategy 3: heuristic — scan all links for one mentioning "chapter".
    candidates = []
    for a in soup.find_all("a", limit=500):
        text = a.get_text(strip=True)
        if text and "chapter" in text.lower() and len(text) < 120:
            candidates.append(text)

    if candidates:
        return candidates[0]

    return None
