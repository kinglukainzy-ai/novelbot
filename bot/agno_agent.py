"""
agno_agent.py - optional natural-language layer on top of the Brain.

This is ONLY used when the user explicitly sends /ask <something>.
Every other command (/add, /list, /status, etc.) goes straight through
brain.py's deterministic parser and never touches an LLM - so the bot
stays free and predictable for normal use.

The Agno agent's "tools" are just thin wrappers that re-assemble the
exact same slash-command strings and hand them to brain.handle(). This
means there's only one real implementation of the logic (in brain.py);
the LLM's only job is figuring out which command(s) to call from a
natural-language sentence.

Uses Groq's free-tier API as the model (https://console.groq.com - free
API key, generous limits for personal use, no cost as long as you stay
within them). To use a different free-tier provider (e.g. Gemini), only
this file needs to change.
"""
import os

_agent_cache = {}


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
        """Get the recent log of update events (new chapters, episodes, status changes)."""
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

    return [
        add_novel, add_anime, list_library, set_status,
        rate_item, add_tag, remove_item, check_for_updates,
        get_history, get_stats, set_progress, set_note,
        find_items, get_recent, get_broken,
    ]


def get_agent(brain):
    """Lazily builds and caches a single Agno agent bound to this brain."""
    if "agent" in _agent_cache:
        return _agent_cache["agent"]

    if not os.getenv("GROQ_API_KEY"):
        return None

    from agno.agent import Agent
    from agno.models.groq import Groq

    model_id = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    agent = Agent(
        model=Groq(id=model_id),
        tools=_build_tools(brain),
        instructions=[
            "You manage the user's personal novel/anime tracking library.",
            "Always use the provided tools to actually perform actions - "
            "never claim you did something without calling the matching tool.",
            "Keep replies short and to the point, suitable for a chat message.",
            "If a request is ambiguous (e.g. which item id), ask a brief "
            "clarifying question instead of guessing.",
        ],
        markdown=False,
    )
    _agent_cache["agent"] = agent
    return agent


def ask(brain, text: str) -> str:
    """Entry point called by brain.py's /ask command."""
    agent = get_agent(brain)
    if agent is None:
        return (
            "Natural-language mode isn't set up yet. Get a free Groq API key "
            "at https://console.groq.com/keys and set GROQ_API_KEY in your .env "
            "file, then restart the bot."
        )
    try:
        response = agent.run(text)
        content = response.get_content_as_string()
        return content or "(no response)"
    except Exception as e:
        return f"Natural-language request failed: {e}"
