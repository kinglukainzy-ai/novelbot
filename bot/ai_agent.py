"""
ai_agent.py - optional natural-language layer on top of the Brain.

This is ONLY used when the user explicitly sends /ask <something>.
Every other command (/add, /list, /status, etc.) goes straight through
brain.py's deterministic parser and never touches an LLM - so the bot
stays free and predictable for normal use.

The tools below are just thin wrappers that re-assemble the exact same
slash-command strings and hand them to brain.handle(). This means there's
only one real implementation of the logic (in brain.py); the LLM's only
job is figuring out which tool(s) to call from a natural-language
sentence, including read-only questions about the library/database
(counts, broken items, history, a specific item's details, etc).

Uses Gemini's native SDK (google-genai) directly - no agent framework.
Gemini's automatic function calling handles the "which tool, with what
arguments" loop on its own when you pass plain Python functions as tools.

Free API key: https://aistudio.google.com/apikey - generous free-tier
limits for personal use, no card required.
"""
import os

_client_cache = {}


def _get_client():
    if "client" in _client_cache:
        return _client_cache["client"]
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    from google import genai
    client = genai.Client(api_key=api_key)
    _client_cache["client"] = client
    return client


def _build_tools(brain):
    def add_novel(title: str, url: str = "", selector: str = "") -> str:
        """Track a new novel by title. If url is omitted, looks it up
        automatically on NovelFire (works for most popular web novels - try
        this first). Only pass url (and optionally selector) when the user
        gives a specific link, or NovelFire doesn't have it."""
        if not url:
            return brain.handle(f"/add novel {title}")
        sel_part = f" | {selector}" if selector else ""
        return brain.handle(f"/add novel {title} | {url}{sel_part}")

    def add_anime(title: str) -> str:
        """Track a new anime by title. Searches AniList for the best match."""
        return brain.handle(f"/add anime {title}")

    def list_library(item_type: str = "", status: str = "") -> str:
        """List tracked items. item_type can be 'novel', 'anime', or empty for both.
        status can be one of reading/watching/on_hold/completed/dropped, or empty for all."""
        parts = " ".join(p for p in [item_type, status] if p)
        return brain.handle(f"/list {parts}".strip())

    def set_status(item_id: int, status: str) -> str:
        """Update an item's status. Must be one of: reading, watching, on_hold, completed, dropped."""
        return brain.handle(f"/status {item_id} {status}")

    def rate_item(item_id: int, score: float, notes: str = "") -> str:
        """Rate a tracked item from 0-10, with optional free-text notes."""
        return brain.handle(f"/rate {item_id} {score} {notes}".strip())

    def add_tag(item_id: int, tag: str) -> str:
        """Add a free-form tag to a tracked item."""
        return brain.handle(f"/tag {item_id} {tag}")

    def remove_item(item_id: int) -> str:
        """Permanently stop tracking an item."""
        return brain.handle(f"/remove {item_id}")

    def check_for_updates() -> str:
        """Force an immediate check for new chapters/episodes across the whole library."""
        return brain.handle("/check")

    def get_history() -> str:
        """Get the recent log of update events (new chapters, episodes, status changes,
        scraper breaks/recoveries, AI-fallback usage, selector resets, etc)."""
        return brain.handle("/history")

    def get_stats() -> str:
        """Get quick counts of the library by type and status."""
        return brain.handle("/stats")

    def set_progress(item_id: int, current: int, total: int = 0) -> str:
        """Set chapter/episode progress for an item. total is optional."""
        total_part = f" {total}" if total else ""
        return brain.handle(f"/progress {item_id} {current}{total_part}")

    def set_note(item_id: int, text: str) -> str:
        """Add or update notes on an item without changing its rating."""
        return brain.handle(f"/note {item_id} {text}")

    def find_items(query: str) -> str:
        """Search the library by title (case-insensitive)."""
        return brain.handle(f"/find {query}")

    def get_recent(days: int = 7) -> str:
        """Show items that received updates (new chapters/episodes) in the last N days."""
        return brain.handle(f"/recent {days}")

    def get_broken() -> str:
        """List items whose scrapers are currently broken."""
        return brain.handle("/broken")

    def get_item_details(item_id: int) -> str:
        """Get every stored field for one item by id - url, selector, status,
        rating, notes, tags, progress, last_snapshot, broken flag, last_checked,
        timestamps, etc. Use this whenever the user asks something specific
        about one item that the other tools' summaries don't already show
        (e.g. 'what URL is #3 using', 'what's the selector for my Solo
        Leveling tracker', 'when did #7 last get checked')."""
        item = brain.db.get_item(item_id)
        if not item:
            return f"No item with id {item_id}."
        return "\n".join(f"{k}: {v}" for k, v in item.items())

    def get_library_data(item_type: str = "", status: str = "") -> str:
        """Get the FULL library as raw structured data (one line per item:
        id, type, title, status, rating, progress, tags, last_snapshot,
        broken), not the pretty-printed cards from list_library. Use this
        whenever the user asks you to rank, sort, filter, compare, or do
        any math/aggregation across multiple items - e.g. 'lowest rated',
        'which novels have no rating yet', 'how many have I completed',
        'what genres am I reading'. Do the sorting/filtering/counting
        yourself from this data; don't say you can't if this tool gives you
        everything you need to answer. item_type can be 'novel', 'anime',
        or empty for both. status filters the same way as list_library."""
        items = brain.db.list_items(item_type or None, status or None)
        if not items:
            return "Library is empty."
        lines = []
        for it in items:
            lines.append(
                f"id={it['id']} type={it['type']} title={it['title']!r} "
                f"status={it['status']} rating={it.get('rating')} "
                f"progress={it.get('progress_current')}/{it.get('progress_total')} "
                f"tags={it.get('tags') or ''} "
                f"last_snapshot={it.get('last_snapshot') or ''} "
                f"broken={bool(it.get('broken'))}"
            )
        return "\n".join(lines)

    def run_health_check() -> str:
        """Run the bot's own health check (DB connectivity, scheduler status, etc).
        Use this if the user asks whether the bot itself is healthy/working."""
        return brain.handle("/health")

    def force_fix_scraper(item_id: int) -> str:
        """Force an immediate retry of a broken novel's scraper (selector,
        then the chapter heuristic, then the AI fallback as a last resort).
        Use this when the user asks to fix/retry/unbreak a specific tracked
        novel right now, instead of waiting for the next scheduled check."""
        return brain.handle(f"/fix {item_id}")

    return [
        add_novel, add_anime, list_library, set_status,
        rate_item, add_tag, remove_item, check_for_updates,
        get_history, get_stats, set_progress, set_note,
        find_items, get_recent, get_broken, get_item_details,
        get_library_data, run_health_check, force_fix_scraper,
    ]


SYSTEM_INSTRUCTION = (
    "You are a versatile assistant over the user's personal novel/anime "
    "tracking library and the bot that runs it. "
    "Always use the provided tools to actually perform actions or look up "
    "real data - never claim you did something, or state a fact about the "
    "library/database, without calling the matching tool first. "
    "You can answer ANY question about the bot's data: library contents, "
    "an item's full stored details (URL, selector, progress, notes, broken "
    "status, timestamps...), history of events, stats, recently updated "
    "items, broken scrapers, or the bot's own health. "
    "For anything involving ranking, sorting, filtering, comparing, or "
    "counting across multiple items (lowest/highest rated, unrated items, "
    "how many completed, which novels are broken, etc), call "
    "get_library_data to get the full library as structured data, then do "
    "the sorting/filtering/math yourself - don't say you can't answer just "
    "because there's no single tool that already does the ranking for you. "
    "RECOMMENDATIONS: When the user asks for book/novel/anime recommendations "
    "or what they should read/watch next, always call get_library_data first "
    "to understand their taste - look at their highest-rated items, tags, "
    "genres, and notes. Then use your own general knowledge of the web novel, "
    "light novel, manhwa, and anime space to suggest titles they are NOT "
    "already tracking that fit those patterns. Be specific: name the title, "
    "give a one-sentence reason tied directly to something in their library "
    "(e.g. 'you rated X highly and this has the same system/regression "
    "themes'). Suggest 3-5 titles. If they immediately ask to add one, call "
    "add_novel or add_anime right away without asking again. "
    "If a question genuinely needs data no tool provides, say so plainly "
    "rather than guessing. "
    "Keep replies short and to the point, suitable for a chat message. "
    "If a request is ambiguous (e.g. which item id), ask a brief "
    "clarifying question instead of guessing."
)


def ask(brain, text: str) -> str:
    """Entry point called by brain.py's /ask command.

    Retries up to 3 times on transient server errors (503 UNAVAILABLE,
    429 RESOURCE_EXHAUSTED) with exponential back-off before giving up.
    """
    client = _get_client()
    if client is None:
        return (
            "Natural-language mode isn't set up yet. Get a free Gemini API key "
            "at https://aistudio.google.com/apikey and set GEMINI_API_KEY in your "
            ".env file, then restart the bot."
        )

    import time

    _RETRYABLE = ("503", "503 UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                  "UNAVAILABLE", "overloaded")
    MAX_RETRIES = 3
    BACKOFF_BASE = 2  # seconds; doubles each attempt: 2s, 4s, 8s

    from google.genai import types

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=_build_tools(brain),
    )

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=text,
                config=config,
            )
            return response.text or "(no response)"
        except Exception as e:
            last_err = e
            err_str = str(e)
            is_retryable = any(token.lower() in err_str.lower()
                               for token in _RETRYABLE)
            if is_retryable and attempt < MAX_RETRIES:
                wait = BACKOFF_BASE ** attempt
                time.sleep(wait)
                continue
            # Non-retryable error, or exhausted retries — bail out
            break

    # Friendly message depending on whether it was an overload
    err_str = str(last_err)
    if any(token.lower() in err_str.lower() for token in _RETRYABLE):
        return (
            "Gemini's servers are overloaded right now (503). "
            "Try again in a minute — this is on Google's side, not the bot."
        )
    return f"Natural-language request failed: {last_err}"