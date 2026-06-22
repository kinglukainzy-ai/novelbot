"""
brain.py - the one place all command logic lives. Telegram and WhatsApp
adapters just pass raw text in here and send back whatever string comes out.
This is what makes "one library across both apps" work.
"""
from bot.storage import Storage
from bot.scraper import fetch_snapshot, ScrapeError
from bot import anilist

VALID_STATUSES = ["reading", "watching", "on_hold", "completed", "dropped"]

HELP_TEXT = """\
*Commands*
/add novel <title> | <url> | [css selector]   - track a novel (selector optional but recommended)
/add anime <title>                            - search & track an anime via AniList
/list [novel|anime] [status]                  - show your library
/status <id> <status>                         - reading/watching/on_hold/completed/dropped
/rate <id> <score> [notes]                    - rate 0-10 with optional notes
/tag <id> <tag>                               - add a tag
/remove <id>                                  - stop tracking
/check                                          - force a check right now
/history                                        - recent updates log
/stats                                          - quick counts
/health                                         - system health status
/ask <anything>                                 - natural language mode (needs GROQ_API_KEY, see README)
/help                                           - this message

Tip: items use | as a separator for /add novel, e.g.
/add novel Omniscient Reader | https://example.com/orv | div.latest-chapter
"""


class Brain:
    def __init__(self, db_path: str, notifier=None):
        self.db = Storage(db_path)
        # notifier(text) -> sends a message out to the owning user(s).
        # Set by main.py once both adapters exist.
        self.notifier = notifier

    # ---------------- public entrypoint ----------------

    def handle(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "Send /help to see what I can do."

        parts = text.split(" ", 1)
        cmd = parts[0].lower().lstrip("/")
        rest = parts[1] if len(parts) > 1 else ""

        handlers = {
            "start": self._cmd_help,
            "help": self._cmd_help,
            "add": self._cmd_add,
            "list": self._cmd_list,
            "status": self._cmd_status,
            "rate": self._cmd_rate,
            "tag": self._cmd_tag,
            "remove": self._cmd_remove,
            "check": self._cmd_check,
            "history": self._cmd_history,
            "stats": self._cmd_stats,
            "health": self._cmd_health,
            "ask": self._cmd_ask,
        }
        fn = handlers.get(cmd)
        if not fn:
            return "I didn't recognize that. Send /help to see commands."
        try:
            return fn(rest)
        except Exception as e:
            return f"Something went wrong: {e}"

    # ---------------- commands ----------------

    def _cmd_help(self, _):
        return HELP_TEXT

    def _cmd_add(self, rest):
        sub, _, rest = rest.partition(" ")
        sub = sub.lower()
        if sub == "novel":
            parts = [p.strip() for p in rest.split("|")]
            if len(parts) < 2:
                return "Format: /add novel <title> | <url> | [css selector]"
            title, url = parts[0], parts[1]
            selector = parts[2] if len(parts) > 2 and parts[2] else None
            existing = self.db.find_by_url(url)
            if existing:
                return f"That URL is already tracked as #{existing['id']}: {existing['title']}"
            try:
                snap = fetch_snapshot(url, selector)
            except ScrapeError as e:
                return (
                    f"Added, but I couldn't verify it right away: {e}\n"
                    "It'll be marked broken until fixed."
                )
            item_id = self.db.add_item("novel", title, url=url, selector=selector)
            self.db.update_item(item_id, last_snapshot=snap, broken=0)
            return f"Tracking novel #{item_id}: {title}\nCurrent latest: {snap}"

        elif sub == "anime":
            if not rest.strip():
                return "Format: /add anime <title>"
            results = anilist.search_anime(rest.strip())
            if not results:
                return f"No anime found matching '{rest.strip()}'"
            top = results[0]
            item_id = self.db.add_item("anime", top["title"], anilist_id=top["id"], status="watching")
            state = anilist.get_anime_state(top["id"])
            self.db.update_item(item_id, last_snapshot=state["snapshot"])
            others = ", ".join(r["title"] for r in results[1:4])
            extra = f"\n(Other matches if this is wrong: {others})" if others else ""
            return f"Tracking anime #{item_id}: {state['title']}\nCurrent: {state['snapshot']}{extra}"
        else:
            return "Use: /add novel ... or /add anime ..."

    def _cmd_list(self, rest):
        args = rest.split()
        type_ = None
        status = None
        for a in args:
            if a.lower() in ("novel", "anime"):
                type_ = a.lower()
            elif a.lower() in VALID_STATUSES:
                status = a.lower()
        items = self.db.list_items(type_=type_, status=status)
        if not items:
            return "Nothing here yet. Use /add novel or /add anime to start."
        lines = []
        for it in items:
            flag = " WARNING:BROKEN" if it.get("broken") else ""
            rating = f" ({it['rating']}/10)" if it.get("rating") else ""
            lines.append(f"#{it['id']} [{it['type']}] {it['title']} - {it['status']}{rating}{flag}")
        return "\n".join(lines)

    def _cmd_status(self, rest):
        parts = rest.split()
        if len(parts) < 2:
            return f"Format: /status <id> <{'|'.join(VALID_STATUSES)}>"
        try:
            item_id = int(parts[0])
        except ValueError:
            return "ID must be a number."
        new_status = parts[1].lower()
        if new_status not in VALID_STATUSES:
            return f"Status must be one of: {', '.join(VALID_STATUSES)}"
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        self.db.update_item(item_id, status=new_status)
        self.db.log_event(item_id, "status_change", new_status)
        return f"#{item_id} {item['title']} -> {new_status}"

    def _cmd_rate(self, rest):
        parts = rest.split(" ", 2)
        if len(parts) < 2:
            return "Format: /rate <id> <score 0-10> [notes]"
        try:
            item_id = int(parts[0])
            score = float(parts[1])
        except ValueError:
            return "ID and score must be numbers."
        notes = parts[2] if len(parts) > 2 else None
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        self.db.update_item(item_id, rating=score, notes=notes)
        return f"Rated #{item_id} {item['title']}: {score}/10" + (f" - {notes}" if notes else "")

    def _cmd_tag(self, rest):
        parts = rest.split(" ", 1)
        if len(parts) < 2:
            return "Format: /tag <id> <tag>"
        try:
            item_id = int(parts[0])
        except ValueError:
            return "ID must be a number."
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        existing = (item.get("tags") or "").split(",") if item.get("tags") else []
        existing = [t.strip() for t in existing if t.strip()]
        new_tag = parts[1].strip()
        if new_tag not in existing:
            existing.append(new_tag)
        self.db.update_item(item_id, tags=",".join(existing))
        return f"Tags for #{item_id}: {', '.join(existing)}"

    def _cmd_remove(self, rest):
        try:
            item_id = int(rest.strip())
        except ValueError:
            return "Format: /remove <id>"
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        self.db.delete_item(item_id)
        return f"Removed #{item_id}: {item['title']}"

    def _cmd_check(self, _):
        results = self.run_check_cycle()
        if not results:
            return "Checked everything - no updates."
        return "\n".join(results)

    def _cmd_history(self, _):
        rows = self.db.recent_history(limit=15)
        if not rows:
            return "No history yet."
        lines = [f"{r['created_at'][:16]} - [{r['type']}] {r['title']}: {r['event']} {r['detail'] or ''}" for r in rows]
        return "\n".join(lines)

    def _cmd_stats(self, _):
        s = self.db.stats()
        lines = [f"Total tracked: {s['total']}"]
        for k, v in s["by_type"].items():
            lines.append(f"  {k}: {v}")
        lines.append("By status:")
        for k, v in s["by_status"].items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def _cmd_health(self, _):
        """Check system health - database connectivity, basic stats, etc."""
        try:
            s = self.db.stats()
            status = "✓ Healthy"
            db_status = "✓ Database OK"
            items_count = s['total']
            lines = [
                status,
                db_status,
                f"Items tracked: {items_count}",
                f"Novels: {s['by_type'].get('novel', 0)}",
                f"Anime: {s['by_type'].get('anime', 0)}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"✗ Health check failed: {e}"

    def _cmd_ask(self, rest):
        if not rest.strip():
            return "Format: /ask <whatever you want to say>"
        from bot import agno_agent  # lazy import - only needed if /ask is used
        return agno_agent.ask(self, rest)

    # ---------------- background check cycle ----------------

    def run_check_cycle(self):
        """
        Called by the scheduler (and by /check). Returns a list of
        human-readable update strings, and fires self.notifier for each
        one if a notifier is set (used by the scheduler for push alerts).
        """
        messages = []
        for item in self.db.all_active_items():
            try:
                if item["type"] == "novel":
                    msg = self._check_novel(item)
                else:
                    msg = self._check_anime(item)
                if msg:
                    messages.append(msg)
            except Exception as e:
                messages.append(f"Error checking #{item['id']} {item['title']}: {e}")
        return messages

    def _check_novel(self, item):
        try:
            snap = fetch_snapshot(item["url"], item.get("selector"))
        except ScrapeError as e:
            if not item.get("broken"):
                self.db.update_item(item["id"], broken=1)
                self.db.log_event(item["id"], "scraper_broken", str(e))
                msg = f"WARNING: Tracking broke for novel #{item['id']} {item['title']}: {e}"
                self._notify(msg)
                return msg
            return None  # already known broken, don't spam

        if item.get("broken"):
            self.db.update_item(item["id"], broken=0)

        if snap != item.get("last_snapshot"):
            self.db.update_item(item["id"], last_snapshot=snap)
            self.db.log_event(item["id"], "new_chapter", snap)
            msg = f"New update for {item['title']}: {snap}"
            self._notify(msg)
            return msg
        return None

    def _check_anime(self, item):
        if not item.get("anilist_id"):
            return None  # no AniList ID - skip to avoid bad API requests
        state = anilist.get_anime_state(item["anilist_id"])
        if state["snapshot"] != item.get("last_snapshot"):
            self.db.update_item(item["id"], last_snapshot=state["snapshot"])
            self.db.log_event(item["id"], "new_episode", state["snapshot"])
            msg = f"New episode for {item['title']}: {state['snapshot']}"
            self._notify(msg)
            return msg
        return None

    def _notify(self, msg):
        if self.notifier:
            try:
                self.notifier(msg)
            except Exception:
                pass
