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
sentence, including read-only questions about the library/database,
recommendations, and multi-turn conversations.

ORCHESTRATION
-------------
The local Ollama model (same one used for scraper tier 3) drives the
tool-calling loop whenever it's configured/reachable - it decides which
tools to call, executes them, and answers, entirely offline. Gemini is
reduced to a single tool (web_lookup) that the local model can reach for
ONLY when a question genuinely needs live external information none of
the other tools can provide - general knowledge for recommendations,
current real-world facts, etc. This means the vast majority of /ask
traffic (library questions, ratings, status changes, recommendations
based on your own data) no longer depends on Gemini being up at all.

If Ollama isn't configured, /ask falls back to the original all-Gemini
path so it still works without any local setup.

CONVERSATION MEMORY
-------------------
Each user_id gets a short rolling history (up to HISTORY_TURNS turns),
stored in a neutral {"role": "user"|"assistant", "content": "..."} shape
usable by both backends. This means:
  - After the bot asks "Have you watched X?", the user can reply with just
    "yes" or "no" and the context is preserved.
  - "Add the first one" after a recommendation list works correctly.
  - /ask clear  resets the conversation history for the current user.

Free Gemini API key (only needed for the web_lookup escape hatch and the
no-Ollama fallback path): https://aistudio.google.com/apikey
"""
import inspect
import logging
import os
import time
import requests
from collections import deque

logger = logging.getLogger("ai_agent")

# ---------------------------------------------------------------------------
# Client cache
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Per-user conversation history
# Neutral shape - {"role": "user"|"assistant", "content": "..."} - works for
# both the Ollama path and (after a small conversion) the Gemini fallback.
# Keeps the last HISTORY_TURNS *pairs* (user + assistant).
# ---------------------------------------------------------------------------
HISTORY_TURNS = 6
_history: dict[int, deque] = {}


def _get_history(user_id: int) -> list:
    if user_id not in _history:
        _history[user_id] = deque(maxlen=HISTORY_TURNS * 2)
    return list(_history[user_id])


def _append_history(user_id: int, role: str, content: str):
    if user_id not in _history:
        _history[user_id] = deque(maxlen=HISTORY_TURNS * 2)
    _history[user_id].append({"role": role, "content": content})


def clear_history(user_id: int):
    """Wipe the conversation history for this user (called by /ask clear)."""
    _history.pop(user_id, None)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
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
        id, type, title, status, rating, progress, tags, broken), not the
        pretty-printed cards from list_library. Use this whenever the user
        asks you to rank, sort, filter, compare, or do any math/aggregation
        across multiple items - e.g. 'lowest rated', 'which novels have no
        rating yet', 'how many have I completed', 'what genres am I
        reading'. Do the sorting/filtering/counting yourself from this
        data; don't say you can't if this tool gives you everything you
        need to answer. item_type can be 'novel', 'anime', or empty for
        both. status filters the same way as list_library.

        Note: this deliberately leaves out last_snapshot to keep the
        payload light on a large library - call get_item_details(id) for
        a specific item's full detail (including its latest snapshot) once
        you've identified which item(s) the user actually cares about."""
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
                f"broken={bool(it.get('broken'))}"
            )
        return "\n".join(lines)

    def run_health_check() -> str:
        """Run the bot's own health check (DB connectivity, scheduler status, etc).
        Use this if the user asks whether the bot itself is healthy/working."""
        return brain.handle("/health")

    def force_fix_scraper(item_id: int = 0, all_broken: bool = False) -> str:
        """Run the full 4-tier repair pipeline (selector → heuristic → AI
        page-read → AI web search by title). Set all_broken=True when the
        user says things like 'fix everything', 'fix all broken', 'repair
        all' — runs on every broken novel at once. Pass item_id when fixing
        a specific novel. The web search tier works even when the page is
        completely unreachable."""
        if all_broken:
            return brain.handle("/fix broken")
        return brain.handle(f"/fix {item_id}")

    def clear_broken_flag(item_id: int) -> str:
        """Manually clear the broken flag on a novel without attempting
        any scrape. Use when the user says something like 'just mark it
        as fixed', 'dismiss the broken warning', or 'clear the error on #X'.
        Warns the user that if the underlying problem isn't resolved the
        bot will re-mark it broken on the next scheduled check."""
        return brain.handle(f"/fix clear {item_id}")

    def web_lookup(query: str) -> str:
        """Search the live web for current, real-world information that
        none of the other tools can provide - e.g. general facts about a
        book/show/genre not in the library, recent news, or anything that
        needs up-to-date knowledge beyond what you already know. This is
        the ONLY tool that leaves the bot's own data - use it sparingly,
        only when the question genuinely needs live external information.
        Do NOT use this for anything about the user's own tracked library;
        the other tools already cover that with zero cost or delay."""
        return _web_lookup(query)

    return [
        add_novel, add_anime, list_library, set_status,
        rate_item, add_tag, remove_item, check_for_updates,
        get_history, get_stats, set_progress, set_note,
        find_items, get_recent, get_broken, get_item_details,
        get_library_data, run_health_check, force_fix_scraper, clear_broken_flag,
        web_lookup,
    ]


# ---------------------------------------------------------------------------
# System instruction
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTION = (
    "You are Thoth - named for the Egyptian god of writing, knowledge, and "
    "record-keeping. Your job is to be the user's memory for their personal "
    "novel/anime library: you track what they're reading and watching so "
    "they don't have to hold it in their head. "

    "VOICE: dry, precise, a little wry - the tone of someone who keeps "
    "excellent records and finds that mildly satisfying, not someone "
    "playing a character. No 'mortal', no 'ancient scribe' theatrics, no "
    "exclamation points, no roleplay. A passing dry remark is fine "
    "occasionally; never more than one per reply, and never at the expense "
    "of clarity - the data always comes first. If in doubt, say less. "

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
    "give a one-sentence reason tied directly to something in their library. "
    "Suggest 3-5 titles. If they immediately say 'add it' or 'add the first "
    "one', call add_novel or add_anime right away without asking again. "

    "PROACTIVE QUESTIONS: When recommending anime or novels, you may ask the "
    "user if they have already seen/read a specific title you are about to "
    "suggest - their yes/no answer should determine whether you add it as "
    "'completed' (set_status after adding) or leave it as watching/reading. "
    "Ask one question at a time, not a list. Wait for the answer before "
    "proceeding. The conversation history is preserved between /ask messages "
    "so the user can reply with just 'yes', 'no', or 'add it' and you will "
    "have full context. "

    "CONVERSATION FLOW: You have memory of the last several exchanges with "
    "this user. Use it. If the user says 'yes', 'the second one', 'add that', "
    "'never mind', etc., interpret it relative to what was just discussed. "
    "Never ask for clarification you already have from earlier in the thread. "

    "If a question genuinely needs data no tool provides, say so plainly "
    "rather than guessing. "
    "Keep replies short and to the point, suitable for a chat message. "
    "If a request is truly ambiguous and the history doesn't clarify it, "
    "ask one brief clarifying question."
)


# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------
_RETRYABLE = ("503", "503 UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
              "UNAVAILABLE", "overloaded")
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds; doubles each attempt: 2s, 4s, 8s
GEMINI_FALLBACK_MODELS = ["gemini-2.5-flash-lite"]  # tried after the primary model


def _web_lookup(query: str) -> str:
    """Backs the web_lookup tool. Tries the self-hosted SearXNG instance
    first (no API key, no quota - see bot/websearch.py); only falls back
    to Gemini if SEARXNG_URL was never set up, so existing setups that
    haven't migrated yet don't lose the feature outright."""
    from bot import websearch
    if websearch.is_configured():
        result = websearch.search_and_summarize(query)
        if result is not None:
            return result
        # SearXNG is up but this particular query came back empty/failed -
        # don't silently fall through to Gemini here; that would mask a
        # real "no results" as success on a different backend.
        return "(no results found)"
    return _gemini_web_lookup(query)


def _gemini_web_lookup(query: str) -> str:
    """Legacy path to Gemini - only reached when SEARXNG_URL isn't set up.
    Tries the configured model first, with retry/backoff for transient
    overload, then falls back to a second model (separate quota pool)
    before giving up."""
    client = _get_client()
    if client is None:
        return "Web lookup unavailable - no GEMINI_API_KEY configured."

    from google.genai import types
    models_to_try = [os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), *GEMINI_FALLBACK_MODELS]
    last_err = None
    for model in models_to_try:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = client.models.generate_content(
                    model=model,
                    contents=query,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    ),
                )
                return result.text or "(no results found)"
            except Exception as e:
                last_err = e
                err_str = str(e)
                if any(t.lower() in err_str.lower() for t in _RETRYABLE) and attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue
                break  # this model's exhausted its retries - try the next one
    return f"Web lookup failed - Gemini unavailable on all models tried: {last_err}"


# ---------------------------------------------------------------------------
# Ollama tool-calling support
# ---------------------------------------------------------------------------
_PY_TYPE_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}
OLLAMA_MAX_TOOL_ITERATIONS = 6
OLLAMA_CHAT_TIMEOUT = 180

# Generation is the dominant cost on small/CPU-only VMs (each extra output
# token is another full forward pass), so num_predict is the single biggest
# lever we have - bigger than the tool-schema savings below. Plain small talk
# ("hey", "thanks", "lol") never needs a long reply, so it gets a much
# tighter cap than tool-driven turns, which need room to synthesize a tool
# result into prose.
OLLAMA_CHAT_OPTIONS_PLAIN = {"num_predict": 80, "temperature": 0.4}
OLLAMA_CHAT_OPTIONS_TOOLS = {"num_predict": 300, "temperature": 0.4}
# Backwards-compat alias (some callers/tests may still reference this name).
OLLAMA_CHAT_OPTIONS = OLLAMA_CHAT_OPTIONS_TOOLS

# num_ctx bounds how much context the model has to re-process every turn -
# left unset, some models default higher than this bot ever needs (history
# is capped at HISTORY_TURNS*2 short messages + one system prompt), which
# just means slower prompt-eval for no benefit. num_thread pins Ollama to
# every vCPU the VM actually has - left unset it doesn't always use them
# all, which matters a lot on a 1-4 vCPU free-tier box.
OLLAMA_NUM_CTX = 2048
OLLAMA_NUM_THREAD = max(os.cpu_count() or 1, 1)

OLLAMA_KEEP_ALIVE = "30m"


def _ollama_options(use_tools: bool) -> dict:
    base = dict(OLLAMA_CHAT_OPTIONS_TOOLS if use_tools else OLLAMA_CHAT_OPTIONS_PLAIN)
    base["num_ctx"] = OLLAMA_NUM_CTX
    base["num_thread"] = OLLAMA_NUM_THREAD
    return base


def _function_to_ollama_schema(fn) -> dict:
    """Auto-derives an Ollama/OpenAI-style tool schema from a plain Python
    function's signature + docstring, so the same _build_tools() functions
    work for both backends without hand-writing 21 schemas twice."""
    sig = inspect.signature(fn)
    props, required = {}, []
    for name, param in sig.parameters.items():
        ptype = param.annotation if param.annotation is not inspect.Parameter.empty else str
        props[name] = {"type": _PY_TYPE_TO_JSON.get(ptype, "string")}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": (fn.__doc__ or "").strip(),
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


# Words that signal the message actually needs library data / a tool call.
# If none of these appear, we skip attaching the tool schema entirely -
# that schema alone costs ~85s of extra CPU prompt-eval per turn (measured:
# 3s with no tools vs 90s with tools, for the literal same "hey"), so paying
# it for plain small talk is pure waste. This is a heuristic, not perfect -
# if it guesses wrong, the no-tools reply will just be a plain chat answer
# rather than a data lookup; worth widening this list if that happens often.
_TOOL_TRIGGER_WORDS = (
    "add", "list", "track", "status", "rate", "rating", "top", "best",
    "highest", "lowest", "recommend", "suggest", "broken", "fix", "set",
    "remove", "delete", "stats", "history", "progress", "chapter",
    "source", "next", "update", "tag", "note", "library", "reading",
    "watching", "anime", "novel", "book", "find", "search",
)


def _needs_tools(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _TOOL_TRIGGER_WORDS)


# Maps trigger words -> the small set of tool names actually relevant to
# that intent. Measured overhead (time_tool_overhead_subset.py) scales
# super-linearly with tool count on phi4-mini/Ollama: 1 tool ~10s, 5 tools
# ~16s, 21 tools ~72s for the same prompt. So once we know we need *some*
# tool, it's worth narrowing further to just the relevant handful instead
# of always sending all 21 - that's the difference between ~16s and ~72s.
#
# Multiple groups can match; their tool sets are unioned. If nothing
# matches (shouldn't happen if _needs_tools already said yes, but be
# defensive), fall back to the full tool list rather than risk leaving out
# something the model needs.
_TOOL_GROUPS = {
    ("add", "track"): ["add_novel", "add_anime"],
    ("list", "library", "reading", "watching", "book"): ["list_library", "get_library_data"],
    ("rate", "rating"): ["rate_item"],
    ("status",): ["set_status"],
    ("progress", "chapter"): ["set_progress"],
    ("tag",): ["add_tag"],
    ("note",): ["set_note"],
    ("remove", "delete"): ["remove_item"],
    ("find", "search"): ["find_items", "web_lookup"],
    ("top", "best", "highest", "lowest", "recommend", "suggest"): ["get_library_data"],
    ("broken", "fix", "source"): ["force_fix_scraper", "clear_broken_flag", "get_broken"],
    ("stats",): ["get_stats"],
    ("history", "update", "next"): ["get_history", "get_recent", "check_for_updates"],
    ("anime", "novel"): ["add_novel", "add_anime", "get_item_details"],
}


def _select_tool_names(text: str) -> set:
    """Returns the union of tool names relevant to every trigger-word group
    found in text. Empty set means no group matched (caller should fall
    back to the full tool list)."""
    lowered = text.lower()
    selected = set()
    for words, tool_names in _TOOL_GROUPS.items():
        if any(word in lowered for word in words):
            selected.update(tool_names)
    return selected


def _run_ollama_agent(brain, user_id: int, text: str) -> str:
    from bot import local_llm

    t_start = time.monotonic()
    use_tools = _needs_tools(text)
    if use_tools:
        all_tools = _build_tools(brain)
        selected_names = _select_tool_names(text)
        if selected_names:
            py_tools = [fn for fn in all_tools if fn.__name__ in selected_names]
        else:
            # _needs_tools said yes but no group matched closely enough -
            # safer to send everything than risk missing the right tool.
            py_tools = all_tools
    else:
        py_tools = []
    tool_map = {fn.__name__: fn for fn in py_tools}
    ollama_tools = [_function_to_ollama_schema(fn) for fn in py_tools]

    history = _get_history(user_id)
    messages = ([{"role": "system", "content": SYSTEM_INSTRUCTION}]
                + history + [{"role": "user", "content": text}])

    model = os.getenv("OLLAMA_MODEL", "phi4-mini")
    host = local_llm._ollama_host()
    logger.info(
        f"[timing] /ask {text[:30]!r}: setup done at +{time.monotonic()-t_start:.2f}s "
        f"(use_tools={use_tools}, n_tools={len(ollama_tools)})"
    )

    for iteration in range(OLLAMA_MAX_TOOL_ITERATIONS):
        try:
            t_call = time.monotonic()
            resp = requests.post(
                f"{host}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    **({"tools": ollama_tools} if ollama_tools else {}),
                    "stream": False,
                    "options": _ollama_options(use_tools=bool(ollama_tools)),
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                },
                timeout=OLLAMA_CHAT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                f"[timing] /ask {text[:30]!r}: ollama call #{iteration} took "
                f"{time.monotonic()-t_call:.2f}s (total so far {time.monotonic()-t_start:.2f}s)"
            )
        except (requests.RequestException, ValueError) as e:
            return f"Local model error: {e}"

        msg = data.get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            reply = (msg.get("content") or "").strip() or "(no response)"
            _append_history(user_id, "user", text)
            _append_history(user_id, "assistant", reply)
            return reply

        messages.append(msg)
        for call in tool_calls:
            fn_name = (call.get("function") or {}).get("name")
            fn_args = (call.get("function") or {}).get("arguments") or {}
            fn = tool_map.get(fn_name)
            if not fn:
                result = f"Unknown tool: {fn_name}"
            else:
                try:
                    result = fn(**fn_args)
                except Exception as e:
                    result = f"Tool error calling {fn_name}: {e}"
            messages.append({"role": "tool", "content": str(result), "name": fn_name or ""})

    return "I made too many tool calls in a row without reaching an answer - try rephrasing the question."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def ask(brain, text: str, user_id: int = 0) -> str:
    """Entry point called by brain.py's /ask command.

    user_id - used to maintain per-user conversation history so multi-turn
    exchanges (recommendations, yes/no questions, etc.) work correctly.
    Pass 0 if the caller doesn't have a user id (shared single-user history).

    Special command: if text.strip().lower() == 'clear', wipes history and
    returns a confirmation message without hitting any API.

    Routing: if a local Ollama model is configured/reachable, it drives the
    entire tool-calling loop and Gemini is only touched via the web_lookup
    tool, and only when the local model decides it's actually needed. If no
    local model is configured, falls back to the original all-Gemini path
    so /ask still works without any local setup.
    """
    if text.strip().lower() == "clear":
        clear_history(user_id)
        return "Conversation history cleared. Starting fresh."

    t0 = time.monotonic()
    from bot import local_llm
    configured = local_llm.is_configured()
    logger.info(
        f"[timing] /ask {text[:30]!r}: is_configured() check took "
        f"{time.monotonic()-t0:.2f}s (configured={configured})"
    )
    if configured:
        return _run_ollama_agent(brain, user_id, text)

    return _ask_gemini(brain, text, user_id)


def _ask_gemini(brain, text: str, user_id: int = 0) -> str:
    """Fallback path used only when no local Ollama model is configured -
    everything (orchestration AND any live lookups) runs through Gemini,
    same as before the local-first split existed."""
    client = _get_client()
    if client is None:
        return (
            "Natural-language mode isn't set up yet. Get a free Gemini API key "
            "at https://aistudio.google.com/apikey and set GEMINI_API_KEY in your "
            ".env file, then restart the bot. (Or set up a local Ollama model - "
            "see OLLAMA_HOST/OLLAMA_MODEL in .env - so /ask doesn't depend on "
            "Gemini at all for most questions.)"
        )

    from google.genai import types

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=_build_tools(brain),
    )

    # Convert neutral history ({"role":"user"/"assistant","content":...}) to
    # Gemini's contents shape ({"role":"user"/"model","parts":[{"text":...}]}).
    history = _get_history(user_id)
    contents = [
        {"role": ("model" if h["role"] == "assistant" else "user"),
         "parts": [{"text": h["content"]}]}
        for h in history
    ] + [{"role": "user", "parts": [{"text": text}]}]

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            reply = response.text or "(no response)"

            _append_history(user_id, "user", text)
            _append_history(user_id, "assistant", reply)

            return reply

        except Exception as e:
            last_err = e
            err_str = str(e)
            is_retryable = any(token.lower() in err_str.lower()
                               for token in _RETRYABLE)
            if is_retryable and attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            break

    err_str = str(last_err)
    if any(token.lower() in err_str.lower() for token in _RETRYABLE):
        return (
            "Gemini's servers are overloaded right now (503). "
            "Try again in a minute — this is on Google's side, not the bot. "
            "(Setting up a local Ollama model would make /ask work even when "
            "this happens - see OLLAMA_HOST/OLLAMA_MODEL in .env.)"
        )
    return f"Natural-language request failed: {last_err}"