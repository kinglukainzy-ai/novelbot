"""
novelfire.py - search novelfire.net for novels and extract the latest chapter.

Used by brain.py's /add novel <title> (name-only mode) to auto-find a novel
on NovelFire without the user needing to supply a URL or CSS selector.

Also provides probe_next_chapter() - a "tier 0" check that runs before any
scraping at all. NovelFire chapter URLs are sequential and numeric
(novelfire.net/book/<slug>/chapter-<n>), so once we know the current
chapter number, checking for an update is just "does chapter N+1 exist" -
one direct request, no text-parsing ambiguity, no selector to go stale. A
clean 404 on that request *also* proves the site itself is reachable, so
the common "nothing new yet" steady-state case can skip the listing-page
scrape (tiers 1-2) entirely too, not just the AI tiers.
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


class ProbeAmbiguous(Exception):
    """The probe request itself failed (timeout, DNS, 5xx) - this does NOT
    mean 'no new chapter', it means we genuinely couldn't tell. Callers
    should fall through to the normal scrape pipeline rather than guessing."""
    pass


def _book_base(url: str) -> str | None:
    """Returns the book's base URL (no /chapters suffix) if this is a
    NovelFire book URL, else None."""
    if "novelfire.net/book/" not in url:
        return None
    base = url.split("?")[0].rstrip("/")
    if base.endswith("/chapters"):
        base = base[: -len("/chapters")]
    return base


def probe_next_chapter(book_url: str, current_num: int):
    """The cheap, reliable shortcut: instead of re-scraping the book's
    listing page and parsing fuzzy text every cycle, just ask for chapter
    current_num+1 directly. NovelFire chapter URLs are sequential and
    numeric (.../book/<slug>/chapter-<n>), so this is a single deterministic
    HTTP request with no text-matching ambiguity at all.

    Returns (True, chapter_text) if chapter current_num+1 exists - a
    confirmed new chapter, no further scraping needed this cycle.
    Returns (False, None) on a clean 404 - this also doubles as proof the
    site itself is reachable (the request succeeded, the chapter just isn't
    there yet), so the normal listing-page scrape can be skipped too.
    Raises ProbeAmbiguous if the request itself failed - genuinely unknown,
    caller should fall through to the normal pipeline rather than guessing.
    """
    base = _book_base(book_url)
    if base is None or not current_num:
        return None  # not a NovelFire url, or no known chapter number yet

    candidate = f"{base}/chapter-{current_num + 1}"
    try:
        resp = requests.get(candidate, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        raise ProbeAmbiguous(f"Couldn't reach {candidate}: {e}")

    if resp.status_code == 404:
        return False, None

    if resp.status_code == 200 and f"chapter-{current_num + 1}" in resp.url:
        soup = BeautifulSoup(resp.text, "html.parser")
        h1 = soup.find("h1")
        title_text = h1.get_text(strip=True) if h1 else None
        return True, (title_text or f"Chapter {current_num + 1}")

    # Redirected somewhere unexpected, weird status code, etc - don't guess.
    raise ProbeAmbiguous(f"Unexpected response probing {candidate}: HTTP {resp.status_code}")


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


class ProbeAmbiguous(Exception):
    """The probe request itself failed (timeout, connection error, weird
    status) - couldn't confirm yes or no. Callers should fall through to
    the normal scraping pipeline rather than guessing."""
    pass


def _book_base(url: str) -> str | None:
    """Returns the book's base URL (no trailing /chapters or similar), or
    None if this isn't a NovelFire book URL at all - the probe only
    applies here, anything else falls through to the normal pipeline."""
    if "novelfire.net/book/" not in url:
        return None
    base = url.split("?")[0].rstrip("/")
    if base.endswith("/chapters"):
        base = base[: -len("/chapters")]
    return base


def probe_next_chapter(book_url: str, current_num: int):
    """Tier 0: checks whether chapter (current_num + 1) exists yet by
    requesting its URL directly, rather than scraping the listing page.

    Returns:
      (True, "Chapter N: <title>") - confirmed, a new chapter exists.
      (False, None)                - confirmed, it doesn't exist yet (and
                                      the site responded normally, so it's
                                      reachable - this is a real "healthy,
                                      nothing new" signal, not just silence).
    Raises ProbeAmbiguous if the request itself failed, or returned
    anything that isn't a clean answer - callers should fall through to
    the normal pipeline in that case rather than guessing either way.
    Returns None outright (not a tuple) if this URL isn't a NovelFire book
    page at all, since the probe simply doesn't apply.
    """
    base = _book_base(book_url)
    if base is None or not current_num:
        return None

    candidate = f"{base}/chapter-{current_num + 1}"
    try:
        resp = requests.get(candidate, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        raise ProbeAmbiguous(f"Couldn't reach {candidate}: {e}")

    if resp.status_code == 404:
        return False, None

    if resp.status_code == 200 and f"chapter-{current_num + 1}" in resp.url:
        soup = BeautifulSoup(resp.text, "html.parser")
        h1 = soup.find("h1")
        title_text = h1.get_text(strip=True) if h1 else None
        snap = f"Chapter {current_num + 1}: {title_text}" if title_text else f"Chapter {current_num + 1}"
        return True, snap

    # Redirected somewhere unexpected, or a non-404 error status - don't
    # guess either way, let the normal pipeline take over this cycle.
    raise ProbeAmbiguous(f"Unexpected response probing {candidate}: HTTP {resp.status_code}")
