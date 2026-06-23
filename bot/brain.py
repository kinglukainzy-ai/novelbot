"""
brain.py - the one place all command logic lives. Telegram and WhatsApp
adapters just pass raw text in here and send back whatever string comes out.
This is what makes "one library across both apps" work.
"""
import os

from bot.storage import Storage
from bot.scraper import fetch_snapshot, fetch_snapshot_ai, fetch_snapshot_websearch, ScrapeError
from bot import anilist

VALID_STATUSES = ["reading", "watching", "on_hold", "completed", "dropped"]

HELP_TEXT = """\
<b>Commands</b>
/add novel &lt;title&gt; — auto-find &amp; track via NovelFire
/add novel &lt;title&gt; | &lt;url&gt; | [css selector] — track a novel from any site
/add anime &lt;title&gt; — search &amp; track via AniList
/list [novel|anime] [status] — show your library
/find &lt;query&gt; — search titles
/status &lt;id&gt; &lt;status&gt; — reading/watching/on_hold/completed/dropped
/progress &lt;id&gt; &lt;current&gt; [total] — set chapter/episode progress
/rate &lt;id&gt; &lt;score&gt; [notes] — rate 0-10 with optional notes
/note &lt;id&gt; &lt;text&gt; — add/update notes without rating
/tag &lt;id&gt; &lt;tag&gt; — add a tag
/remove &lt;id&gt; — stop tracking
/check — force an update check now
/recent [days] — items updated recently (default 7d)
/broken — list items with broken scrapers
/fix &lt;id&gt; — force-retry a broken novel right now (selector → heuristic → AI fallback)
/history — recent event log
/stats — quick counts
/health — system health status
/ask &lt;anything&gt; — natural language (needs GEMINI_API_KEY)
/help — this message

Tip: just a title finds it on NovelFire automatically:
/add novel Omniscient Reader's Viewpoint

Or pin an exact source with | as separator, e.g.
/add novel Omniscient Reader | https://example.com/orv | div.latest-chapter
"""


class Brain:
    def __init__(self, db_path: str, notifier=None):
        self.db = Storage(db_path)
        # notifier(text) -> sends a message out to the owning user(s).
        # Set by main.py once both adapters exist.
        self.notifier = notifier

    # ---------------- public entrypoint ----------------

    def handle(self, text: str, user_id: int = 0) -> str:
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
            "find": self._cmd_find,
            "status": self._cmd_status,
            "progress": self._cmd_progress,
            "rate": self._cmd_rate,
            "note": self._cmd_note,
            "tag": self._cmd_tag,
            "remove": self._cmd_remove,
            "check": self._cmd_check,
            "recent": self._cmd_recent,
            "broken": self._cmd_broken,
            "fix": self._cmd_fix,
            "history": self._cmd_history,
            "stats": self._cmd_stats,
            "health": self._cmd_health,
            "ask": self._cmd_ask,
        }
        fn = handlers.get(cmd)
        if not fn:
            return "I didn't recognize that. Send /help to see commands."
        self._current_user_id = user_id
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
            title = parts[0]
            if not title:
                return ("Format: /add novel &lt;title&gt; | &lt;url&gt; | [css selector]\n"
                         "Or just: /add novel &lt;title&gt; to auto-find it on NovelFire")

            has_url = len(parts) >= 2 and parts[1]
            if has_url:
                # ---- manual mode: title | url | [selector], any site ----
                url = parts[1]
                selector = parts[2] if len(parts) > 2 and parts[2] else None
                existing = self.db.find_by_url(url)
                if existing:
                    return f"That URL is already tracked as #{existing['id']}: {existing['title']}"
                # Add first, then verify - a failed verification still means
                # the item should exist (marked broken), not silently vanish.
                item_id = self.db.add_item("novel", title, url=url, selector=selector)
                try:
                    snap = fetch_snapshot(url, selector)
                except ScrapeError as e:
                    snap = fetch_snapshot_ai(url)
                    if snap is None:
                        self.db.update_item(item_id, broken=1)
                        self.db.log_event(item_id, "scraper_broken", str(e))
                        return (
                            f"Tracking novel #{item_id}: {title}, but I couldn't verify it "
                            f"right away: {e}\n"
                            "It's marked broken until fixed - /broken to see it, /remove to drop it."
                        )
                    self.db.log_event(item_id, "scraper_ai_fallback", "selector/heuristic failed on add, AI found a snapshot")
                self.db.update_item(item_id, last_snapshot=snap, broken=0)
                return f"Tracking novel #{item_id}: {title}\nCurrent latest: {snap}"

            # ---- name-only mode: look it up on NovelFire, like /add anime ----
            from bot import novelfire
            results = novelfire.search_novel(title)
            if not results:
                return (
                    f"Couldn't find '{title}' on NovelFire.\n"
                    "Add it manually instead: /add novel &lt;title&gt; | &lt;url&gt; | [css selector]"
                )
            top = results[0]
            existing = self.db.find_by_url(top["url"]) or self.db.find_by_title(top["title"])
            if existing:
                return f"That's already tracked as #{existing['id']}: {existing['title']}"

            item_id = self.db.add_item("novel", top["title"], url=top["url"])
            snap = novelfire.latest_chapter_snapshot(top["url"])
            self.db.update_item(item_id, last_snapshot=snap, broken=0 if snap else 1)

            others = ", ".join(r["title"] for r in results[1:4] if r["url"] != top["url"])
            extra = f"\n(Other matches if this is wrong: {others})" if others else ""
            status_line = f"Current latest: {snap}" if snap else (
                "Added, but couldn't verify the latest chapter yet - it'll retry on the next check."
            )
            return f"Tracking novel #{item_id}: {top['title']} (found on NovelFire)\n{status_line}{extra}"

        elif sub == "anime":
            if not rest.strip():
                return "Format: /add anime &lt;title&gt;"
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

    @staticmethod
    def _format_item_card(it):
        """Build a mini-card string for a single library item (HTML formatted)."""
        icon = "📖" if it["type"] == "novel" else "📺"
        broken = " ⚠️" if it.get("broken") else ""
        title = it["title"]
        header = f"{icon} #{it['id']} — <b>{title}</b>{broken}"

        details = [f"Status: {it['status']}"]
        if it.get("progress_current"):
            prog = str(it["progress_current"])
            if it.get("progress_total"):
                prog += f"/{it['progress_total']}"
            details.append(f"Progress: {prog}")
        if it.get("rating"):
            details.append(f"Rating: {it['rating']}/10")
        if it.get("last_snapshot"):
            details.append(f"Latest: {it['last_snapshot']}")

        return header + "\n" + "  |  ".join(details)

    PAGE_SIZE = 10

    def _cmd_list(self, rest):
        args = rest.split()
        type_ = None
        status = None
        page = 1
        for a in args:
            if a.lower() in ("novel", "anime"):
                type_ = a.lower()
            elif a.lower() in VALID_STATUSES:
                status = a.lower()
            else:
                try:
                    page = max(1, int(a))
                except ValueError:
                    pass
        items = self.db.list_items(type_=type_, status=status)
        if not items:
            return "Nothing here yet. Use /add novel or /add anime to start."

        total = len(items)
        total_pages = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        page = min(page, total_pages)
        start = (page - 1) * self.PAGE_SIZE
        page_items = items[start : start + self.PAGE_SIZE]

        cards = [self._format_item_card(it) for it in page_items]
        result = "\n\n".join(cards)

        if total_pages > 1:
            # Rebuild the filter part so the footer hint is copy-pasteable
            filter_parts = " ".join(p for p in [type_, status] if p)
            next_hint = f"/list {filter_parts} {page + 1}".strip() if page < total_pages else None
            footer = f"\n\n— Page {page}/{total_pages} ({total} items)"
            if next_hint:
                footer += f"  →  send {next_hint}"
            result += footer

        return result

    def _cmd_status(self, rest):
        parts = rest.split()
        if len(parts) < 2:
            return f"Format: /status &lt;id&gt; &lt;{'|'.join(VALID_STATUSES)}&gt;"
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
            return "Format: /rate &lt;id&gt; &lt;score 0-10&gt; [notes]"
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
            return "Format: /tag &lt;id&gt; &lt;tag&gt;"
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
            return "Format: /remove &lt;id&gt;"
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

    def _cmd_progress(self, rest):
        parts = rest.split()
        if len(parts) < 2:
            return "Format: /progress &lt;id&gt; &lt;current&gt; [total]"
        try:
            item_id = int(parts[0])
            current = int(parts[1])
        except ValueError:
            return "ID and progress must be numbers."
        total = None
        if len(parts) > 2:
            try:
                total = int(parts[2])
            except ValueError:
                return "Total must be a number."
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        fields = {"progress_current": current}
        if total is not None:
            fields["progress_total"] = total
        self.db.update_item(item_id, **fields)
        prog = f"{current}/{total}" if total else str(current)
        return f"#{item_id} {item['title']} progress → {prog}"

    def _cmd_note(self, rest):
        parts = rest.split(" ", 1)
        if len(parts) < 2:
            return "Format: /note &lt;id&gt; &lt;text&gt;"
        try:
            item_id = int(parts[0])
        except ValueError:
            return "ID must be a number."
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        self.db.update_item(item_id, notes=parts[1].strip())
        return f"Notes updated for #{item_id} {item['title']}"

    def _cmd_find(self, rest):
        query = rest.strip()
        if not query:
            return "Format: /find &lt;query&gt;"
        items = self.db.search_items(query)
        if not items:
            return f"No items matching '{query}'"
        cards = [self._format_item_card(it) for it in items]
        return "\n\n".join(cards)

    def _cmd_recent(self, rest):
        try:
            days = int(rest.strip()) if rest.strip() else 7
        except ValueError:
            days = 7
        items = self.db.recently_updated_items(days=days)
        if not items:
            return f"No updates in the last {days} day(s)."
        cards = [self._format_item_card(it) for it in items]
        return f"<b>Updated in the last {days} day(s):</b>\n\n" + "\n\n".join(cards)

    def _cmd_broken(self, _):
        items = self.db.broken_items()
        if not items:
            return "No broken items — everything is healthy! ✓"
        cards = [self._format_item_card(it) for it in items]
        return f"<b>⚠️ Broken items ({len(items)}):</b>\n\n" + "\n\n".join(cards)

    def _cmd_fix(self, rest):
        rest = rest.strip()
        if not rest:
            return (
                "Format:\n"
                "  /fix <id>        — run the full 4-tier repair pipeline right now\n"
                "  /fix clear <id>  — manually clear the broken flag (no scrape attempt)"
            )

        parts = rest.split()

        # ── /fix clear <id> ──────────────────────────────────────────────────
        if parts[0].lower() == "clear":
            if len(parts) < 2:
                return "Format: /fix clear <id>"
            try:
                item_id = int(parts[1])
            except ValueError:
                return "Item id must be a number, e.g. /fix clear 236"
            item = self.db.get_item(item_id)
            if not item:
                return f"No item with id {item_id}."
            if not item.get("broken"):
                return f"#{item_id} {item['title']} isn't marked broken — nothing to clear."
            self.db.update_item(item_id, broken=0)
            self.db.log_event(item_id, "scraper_fixed", "broken flag cleared manually via /fix clear")
            return (
                f"✅ Cleared broken flag for #{item_id} {item['title']}.\n"
                "Note: no scrape was attempted — if the underlying problem isn't fixed "
                "the bot will re-mark it broken on the next scheduled check."
            )

        # ── /fix <id> ─────────────────────────────────────────────────────────
        try:
            item_id = int(parts[0])
        except ValueError:
            return "Item id must be a number, e.g. /fix 236"

        item = self.db.get_item(item_id)
        if not item:
            return f"No item with id {item_id}."
        if item["type"] != "novel":
            return f"#{item_id} {item['title']} isn't a novel — only novels have scrapers to fix."
        if not item.get("url"):
            return f"#{item_id} {item['title']} has no URL on file — nothing to scrape."

        # 4-tier pipeline, same order as a normal check cycle.
        # Reports exactly which tier worked (or all four failed).
        snap = None
        method = None

        # Tier 1: selector
        try:
            snap = fetch_snapshot(item["url"], item.get("selector"))
            method = "selector" if item.get("selector") else "heuristic"
        except ScrapeError:
            pass

        # Tier 2: heuristic (no selector)
        if snap is None and item.get("selector"):
            try:
                snap = fetch_snapshot(item["url"], None)
                method = "heuristic"
            except ScrapeError:
                pass

        # Tier 3: AI reads the page text
        if snap is None:
            snap = fetch_snapshot_ai(item["url"])
            if snap is not None:
                method = "AI page-read"

        # Tier 4: AI web search by title — page itself may be unreachable
        if snap is None:
            snap = fetch_snapshot_websearch(item["title"])
            if snap is not None:
                method = "AI web search"

        if snap is None:
            hint = (
                "Use /fix clear {id} to manually dismiss the broken flag if "
                "you're sure the novel is fine."
            ).format(id=item_id)
            if os.getenv("GEMINI_API_KEY"):
                return (
                    f"❌ Still broken: all 4 tiers failed for #{item_id} {item['title']}.\n"
                    "The page may be permanently down, geo-blocked, or behind a login wall.\n"
                    + hint
                )
            return (
                f"❌ Still broken: #{item_id} {item['title']} — GEMINI_API_KEY not set so "
                "tiers 3 and 4 were skipped.\nSet it and run /fix again, or: " + hint
            )

        self.db.update_item(item_id, last_snapshot=snap, broken=0)
        self.db.log_event(item_id, "scraper_fixed", f"manual /fix via {method}")
        return (
            f"✅ Fixed #{item_id} {item['title']} via {method}.\n"
            f"Latest: {snap}"
        )

    def _cmd_ask(self, rest):
        if not rest.strip():
            return "Format: /ask &lt;whatever you want to say&gt;"
        from bot import ai_agent  # lazy import - only needed if /ask is used
        return ai_agent.ask(self, rest, user_id=getattr(self, '_current_user_id', 0))

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
        snap = None
        selector_failed = False

        # Primary scrape attempt (with selector if set).
        try:
            snap = fetch_snapshot(item["url"], item.get("selector"))
        except ScrapeError:
            selector_failed = True

        # Fallback: if the selector broke, retry without it. Selectors go
        # stale on site redesigns but the URL itself is usually still good.
        if selector_failed and item.get("selector"):
            try:
                snap = fetch_snapshot(item["url"], None)
            except ScrapeError:
                pass  # genuine failure, handled below

        # Last resort: both the selector and the "chapter" heuristic came up
        # empty (e.g. full site redesign). Ask Gemini to find the latest
        # chapter marker in the page text. Only fires when genuinely broken,
        # and is a no-op (returns None) if GEMINI_API_KEY isn't set.
        used_ai_fallback = False
        if snap is None:
            ai_snap = fetch_snapshot_ai(item["url"])
            if ai_snap is not None:
                snap = ai_snap
                used_ai_fallback = True

        # Tier 4: web search by title — fires when the page itself is
        # unreachable (down, geo-blocked, behind JS). Gemini searches live
        # for the novel's latest chapter using only its name.
        used_websearch = False
        if snap is None:
            ws_snap = fetch_snapshot_websearch(item["title"])
            if ws_snap is not None:
                snap = ws_snap
                used_websearch = True

        # Still no snapshot — mark or stay broken.
        if snap is None:
            if not item.get("broken"):
                self.db.update_item(item["id"], broken=1)
                self.db.log_event(item["id"], "scraper_broken", "all 4 tiers failed")
                msg = f"⚠️ Tracking broke for novel #{item['id']} {item['title']}"
                self._notify(msg)
                return msg
            return None  # already known broken, don't spam

        # If we got here with a broken item, it healed itself.
        if item.get("broken"):
            self.db.update_item(item["id"], broken=0)
            if used_websearch:
                recover_detail = "web search fallback found a snapshot"
            elif used_ai_fallback:
                recover_detail = "AI fallback found a snapshot"
            else:
                recover_detail = "auto-healed"
            self.db.log_event(item["id"], "scraper_recovered", recover_detail)
            recovery_msg = f"✅ #{item['id']} {item['title']} is back! Scraper recovered."
            self._notify(recovery_msg)

            # If the selector was the problem, clear it so future checks
            # don't keep failing and falling back every cycle. Don't clear
            # it just because the AI fallback fired - that's a per-cycle
            # cost, not a one-time fix, so leave the selector in place in
            # case the next normal check works again on its own.
            if selector_failed and not used_ai_fallback and not used_websearch:
                self.db.update_item(item["id"], selector=None)
                self.db.log_event(item["id"], "selector_cleared",
                                  "old selector stopped working, cleared")

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
