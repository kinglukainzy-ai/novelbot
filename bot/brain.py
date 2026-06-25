"""
brain.py - the one place all command logic lives. Telegram and WhatsApp
adapters just pass raw text in here and send back whatever string comes out.
This is what makes "one library across both apps" work.
"""
import os
import datetime

from bot.storage import Storage
from bot.scraper import (
    fetch_snapshot, fetch_snapshot_ai, fetch_snapshot_websearch,
    parse_chapter_number, ScrapeError, PageUnreachable,
)
from bot import anilist, novelfire

VALID_STATUSES = ["reading", "watching", "on_hold", "completed", "dropped"]


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or url
    except Exception:
        return url

HELP_TEXT = """\
<b>🪶 Thoth</b> — your novel &amp; anime tracker
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
/set &lt;id&gt; url &lt;new url&gt; — fix a novel's source link in place
/set &lt;id&gt; selector &lt;css|clear&gt; — fix/clear a novel's selector in place
/remove &lt;id&gt; — stop tracking
/check — force an update check now
/next — time until the next scheduled check
/sources — which scraping tiers are currently available
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
    def __init__(self, db_path: str, notifier=None, check_interval_minutes=None):
        self.db = Storage(db_path)
        # notifier(text) -> sends a message out to the owning user(s).
        # Set by main.py once both adapters exist.
        self.notifier = notifier
        # Used by /next to estimate the next scheduled check. Falls back to
        # the env var directly if main.py didn't pass one explicitly, so
        # this never silently breaks the check cycle if left unset.
        self._check_interval_minutes = check_interval_minutes or int(os.getenv("CHECK_INTERVAL_MINUTES", "90"))

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
            "set": self._cmd_set,
            "check": self._cmd_check,
            "next": self._cmd_next_check,
            "sources": self._cmd_sources,
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
                    try:
                        snap = fetch_snapshot_ai(url, title)
                    except PageUnreachable:
                        snap = None
                    if snap is None:
                        self.db.update_item(item_id, broken=1)
                        self.db.log_event(item_id, "scraper_broken", str(e))
                        return (
                            f"Tracking novel #{item_id}: {title}, but I couldn't verify it "
                            f"right away: {e}\n"
                            "It's marked broken until fixed - /broken to see it, /remove to drop it."
                        )
                    self.db.log_event(item_id, "scraper_ai_fallback", "selector/heuristic failed on add, AI found a snapshot")
                self.db.update_item(item_id, last_snapshot=snap, last_chapter_num=parse_chapter_number(snap), broken=0)
                return f"Tracking novel #{item_id}: {title}\nCurrent latest: {snap}"

            # ---- name-only mode: look it up on NovelFire, like /add anime ----
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
            self.db.update_item(item_id, last_snapshot=snap, last_chapter_num=parse_chapter_number(snap) if snap else None,
                                 broken=0 if snap else 1)

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
        """Detailed two-line card (HTML). Used by /find, /broken, /recent -
        contexts showing one item or a short, deliberate list, where the
        extra detail per item is worth the space."""
        icon = "📖" if it["type"] == "novel" else "📺"
        broken = " ⚠️" if it.get("broken") else ""
        header = f"{icon} #{it['id']} — <b>{it['title']}</b>{broken}"

        parts = []
        if it["type"] == "novel":
            label = "Ch."
        else:
            label = "Ep."

        if it.get("progress_current"):
            prog = str(it["progress_current"])
            if it.get("progress_total"):
                prog += f"/{it['progress_total']}"
            parts.append(f"{label} {prog}")

        if it.get("rating"):
            parts.append(f"{it['rating']}/10 ⭐")

        if it.get("last_snapshot") and it["type"] == "novel":
            snap = it["last_snapshot"]
            if len(snap) > 40:
                snap = snap[:40] + "…"
            parts.append(snap)

        line2 = "  |  ".join(parts) if parts else it.get("status", "")
        return header + "\n  " + line2

    @staticmethod
    def _format_item_line(it):
        """One dense line per item - the default /list view dumps every
        tracked item, so density matters more than detail here. Full detail
        is one tap away via /find <title> or /status."""
        icon = "📖" if it["type"] == "novel" else "📺"
        broken = " ⚠️" if it.get("broken") else ""
        label = "ch" if it["type"] == "novel" else "ep"

        bits = []
        if it.get("progress_current"):
            prog = str(it["progress_current"])
            if it.get("progress_total"):
                prog += f"/{it['progress_total']}"
            bits.append(f"{label}.{prog}")
        if it.get("rating"):
            bits.append(f"{it['rating']}/10⭐")
        bits.append(it.get("status", ""))
        detail = ", ".join(b for b in bits if b)

        return f"{icon} #{it['id']} <b>{it['title']}</b>{broken} — {detail}"

    PAGE_SIZE = 15

    def _cmd_list(self, rest):
        args = rest.split()
        filter_type = None
        filter_status = None
        filter_tag = None
        page = 1

        for a in args:
            al = a.lower()
            if al in ("novel", "anime"):
                filter_type = al
            elif al in VALID_STATUSES:
                filter_status = al
            elif al.startswith("tag:"):
                filter_tag = al[4:]
            else:
                try:
                    page = max(1, int(a))
                except ValueError:
                    pass

        all_items = self.db.list_items(type_=filter_type, status=filter_status)

        # tag filter (post-query since storage doesn't expose it)
        if filter_tag:
            all_items = [i for i in all_items
                         if filter_tag.lower() in (i.get("tags") or "").lower()]

        if not all_items:
            return "Nothing matches. Use /add novel or /add anime to start tracking."

        total = len(all_items)
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = min(page, total_pages)
        start = (page - 1) * self.PAGE_SIZE
        page_items = all_items[start: start + self.PAGE_SIZE]

        # ── group by type when no type filter ─────────────────────────────────
        if filter_type is None:
            novels = [i for i in page_items if i["type"] == "novel"]
            anime  = [i for i in page_items if i["type"] == "anime"]
            sections = []

            for group, label, icon in [(novels, "Novels", "📖"), (anime, "Anime", "📺")]:
                if not group:
                    continue
                broken_count = sum(1 for i in group if i.get("broken"))
                broken_note = f"  ⚠️ {broken_count} broken" if broken_count else ""
                header = f"{icon} <b>{label}</b> ({len(group)}){broken_note}"
                lines = [Brain._format_item_line(i) for i in group]
                sections.append(header + "\n" + "\n".join(lines))

            result = "\n\n".join(sections)
        else:
            icon = "📖" if filter_type == "novel" else "📺"
            label = "Novels" if filter_type == "novel" else "Anime"
            broken_count = sum(1 for i in all_items if i.get("broken"))
            broken_note = f"  ⚠️ {broken_count} broken" if broken_count else ""
            header = f"{icon} <b>{label}</b> ({total}){broken_note}"
            lines = [Brain._format_item_line(i) for i in page_items]
            result = header + "\n" + "\n".join(lines)

        if total_pages > 1:
            filter_parts = " ".join(p for p in [filter_type, filter_status] if p)
            footer = f"\n\n— Page {page}/{total_pages} ({total} total)"
            if page < total_pages:
                footer += f"  →  /list {filter_parts} {page + 1}".strip()
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
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        notes = parts[2] if len(parts) > 2 else None
        fields = {"rating": score}
        if notes:
            # Only touch notes if new ones were actually given - rating
            # alone shouldn't blank out notes written earlier.
            fields["notes"] = notes
        self.db.update_item(item_id, **fields)
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

    def _cmd_set(self, rest):
        """Fix a novel's url or selector in place - no need to /remove and
        re-/add just because a site moved or a selector went stale (which
        would also lose rating/tags/notes/progress)."""
        parts = rest.split(" ", 2)
        if len(parts) < 2:
            return ("Format: /set &lt;id&gt; url &lt;new url&gt;\n"
                     "or: /set &lt;id&gt; selector &lt;new css selector&gt;\n"
                     "or: /set &lt;id&gt; selector clear")
        try:
            item_id = int(parts[0])
        except ValueError:
            return "ID must be a number."
        field = parts[1].lower()
        item = self.db.get_item(item_id)
        if not item:
            return f"No item #{item_id}"
        if item["type"] != "novel":
            return "Only novels have a url/selector to fix - anime is tracked via AniList automatically."

        if field == "url":
            if len(parts) < 3 or not parts[2].strip():
                return "Format: /set &lt;id&gt; url &lt;new url&gt;"
            new_url = parts[2].strip()
            dup = self.db.find_by_url(new_url)
            if dup and dup["id"] != item_id:
                return f"That URL is already tracked as #{dup['id']}: {dup['title']}"
            self.db.update_item(item_id, url=new_url, broken=0, last_broken_retry_at=None)
            self.db.log_event(item_id, "url_updated", new_url)
            try:
                snap = fetch_snapshot(new_url, item.get("selector"))
            except ScrapeError as e:
                self.db.update_item(item_id, broken=1)
                return (
                    f"URL updated for #{item_id} {item['title']}, but I still couldn't verify it: {e}\n"
                    "Still marked broken - try /set &lt;id&gt; selector &lt;css&gt; if the page "
                    "loaded but nothing matched."
                )
            self.db.update_item(item_id, last_snapshot=snap, last_chapter_num=parse_chapter_number(snap), broken=0)
            return f"URL updated for #{item_id} {item['title']}\nCurrent latest: {snap}"

        elif field == "selector":
            raw = parts[2].strip() if len(parts) > 2 else ""
            new_selector = None if raw.lower() in ("", "clear", "none") else raw
            self.db.update_item(item_id, selector=new_selector, broken=0, last_broken_retry_at=None)
            self.db.log_event(item_id, "selector_updated", new_selector or "(cleared)")
            try:
                snap = fetch_snapshot(item["url"], new_selector)
            except ScrapeError as e:
                self.db.update_item(item_id, broken=1)
                return f"Selector updated for #{item_id} {item['title']}, but I still couldn't verify it: {e}"
            self.db.update_item(item_id, last_snapshot=snap, last_chapter_num=parse_chapter_number(snap), broken=0)
            return f"Selector updated for #{item_id} {item['title']}\nCurrent latest: {snap}"

        else:
            return ("Format: /set &lt;id&gt; url &lt;new url&gt;\n"
                     "or: /set &lt;id&gt; selector &lt;new css selector|clear&gt;")

    def _cmd_next_check(self, _):
        last_at = self.db.get_setting("last_check_at")
        interval_min = self.db.get_setting("check_interval_minutes")
        if not last_at or not interval_min:
            return "No scheduled check has run yet - one will kick off shortly after the bot starts."
        try:
            last_dt = datetime.datetime.fromisoformat(last_at)
            next_dt = last_dt + datetime.timedelta(minutes=float(interval_min))
            now = datetime.datetime.now(datetime.timezone.utc)
            remaining = next_dt - now
            mins_left = max(0, int(remaining.total_seconds() // 60))
            if mins_left == 0:
                return "A check is due any moment now."
            return f"Last check: {last_at[:16]} UTC\nNext check in ~{mins_left} minute(s)."
        except (ValueError, TypeError):
            return "Couldn't work out the schedule - try /check to run one now."

    def _cmd_sources(self, _):
        from bot import local_llm
        lines = ["<b>Scraper tiers</b>"]
        lines.append("1-2. Selector / heuristic — always available (plain HTTP, no API key)")
        local_ok = local_llm.is_configured()
        lines.append(f"3. Local LLM (page-read) — {'✅ available' if local_ok else '❌ not running (OLLAMA_HOST)'}")
        gemini_ok = bool(os.getenv("GEMINI_API_KEY"))
        lines.append(f"4a. Gemini web search — {'✅ configured' if gemini_ok else '❌ no GEMINI_API_KEY'}")
        tavily_ok = bool(os.getenv("TAVILY_API_KEY"))
        lines.append(f"4b. Tavily web search (backup) — {'✅ configured' if tavily_ok else '❌ no TAVILY_API_KEY'}")
        novels = [i for i in self.db.list_items(type_="novel")]
        domains = sorted({_domain_of(i["url"]) for i in novels if i.get("url")})
        if domains:
            lines.append(f"\nSites currently tracked: {', '.join(domains)}")
        return "\n".join(lines)

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
                "  /fix broken       — run 4-tier pipeline on ALL broken novels at once\n"
                "  /fix <id>         — run the pipeline on one specific novel\n"
                "  /fix clear <id>   — manually clear the broken flag (no scrape attempt)"
            )

        parts = rest.split()

        # ── /fix broken ───────────────────────────────────────────────────────
        if parts[0].lower() == "broken":
            broken = self.db.broken_items()
            if not broken:
                return "No broken items — nothing to fix! ✓"
            novels = [it for it in broken if it["type"] == "novel"]
            skipped = [it for it in broken if it["type"] != "novel"]
            if not novels:
                return (
                    f"No broken novels to fix ({len(skipped)} broken anime item(s) "
                    "can't be scraper-fixed — they track via AniList automatically)."
                )
            fixed = 0
            still_broken = 0
            for item in novels:
                snap, method = None, None
                try:
                    snap = fetch_snapshot(item["url"], item.get("selector"))
                    method = "selector" if item.get("selector") else "heuristic"
                except ScrapeError:
                    pass
                if snap is None and item.get("selector"):
                    try:
                        snap = fetch_snapshot(item["url"], None)
                        method = "heuristic"
                    except ScrapeError:
                        pass
                if snap is None:
                    try:
                        snap = fetch_snapshot_ai(item["url"], item["title"])
                        if snap is not None:
                            method = "local LLM page-read"
                    except PageUnreachable:
                        pass
                if snap is None:
                    snap = fetch_snapshot_websearch(item["title"])
                    if snap is not None:
                        method = "web search"
                if snap is not None:
                    self.db.update_item(item["id"], last_snapshot=snap,
                                         last_chapter_num=parse_chapter_number(snap), broken=0)
                    self.db.log_event(item["id"], "scraper_fixed", f"bulk /fix broken via {method}")
                    fixed += 1
                else:
                    still_broken += 1
            lines = [f"⚙️ Ran the fix pipeline on {len(novels)} broken novel(s): "
                     f"✅ {fixed} fixed, ❌ {still_broken} still broken."]
            if fixed:
                lines.append("/recent for what changed.")
            if still_broken:
                lines.append("/broken for the list, /fix &lt;id&gt; selector clear to manually dismiss any you know are fine.")
            return "\n".join(lines)

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

        # Tier 3: local LLM reads the page text
        page_unreachable = False
        if snap is None:
            try:
                snap = fetch_snapshot_ai(item["url"], item["title"])
                if snap is not None:
                    method = "local LLM page-read"
            except PageUnreachable:
                page_unreachable = True

        # Tier 4: AI web search by title — page itself may be unreachable
        if snap is None:
            snap = fetch_snapshot_websearch(item["title"])
            if snap is not None:
                method = "web search"

        if snap is None:
            hint = (
                "Use /fix clear {id} to manually dismiss the broken flag if "
                "you're sure the novel is fine."
            ).format(id=item_id)
            if page_unreachable:
                return (
                    f"❌ Still broken: #{item_id} {item['title']} — the page itself is unreachable, "
                    "and the web-search fallback also came up empty.\n"
                    "The page may be permanently down, geo-blocked, or behind a login wall.\n"
                    + hint
                )
            if not os.getenv("GEMINI_API_KEY") and not os.getenv("TAVILY_API_KEY"):
                return (
                    f"❌ Still broken: #{item_id} {item['title']} — no GEMINI_API_KEY or "
                    "TAVILY_API_KEY set, so tier 4 (web search) was effectively a no-op.\n"
                    "Set one and run /fix again, or: " + hint
                )
            return (
                f"❌ Still broken: all 4 tiers failed for #{item_id} {item['title']}.\n" + hint
            )

        self.db.update_item(item_id, last_snapshot=snap, last_chapter_num=parse_chapter_number(snap), broken=0)
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

    BROKEN_RETRY_HOURS = 24  # how often to re-run tiers 3/4 on a confirmed-broken item

    def run_check_cycle(self):
        """
        Called by the scheduler (and by /check). Broken/recovered counts are
        aggregated into one numeric summary (not a ping per title - flapping
        items used to mean a wall of messages); new chapters are still
        listed by title, since that's the actual point of the bot. Fires
        self.notifier once at the end with everything combined, if there's
        anything to report.
        """
        self.db.set_setting("last_check_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
        self.db.set_setting("check_interval_minutes", self._check_interval_minutes or 90)

        broken_count = 0
        recovered_count = 0
        new_chapter_lines = []
        error_count = 0

        for item in self.db.all_active_items():
            try:
                if item["type"] == "novel":
                    event = self._check_novel(item)
                else:
                    event = self._check_anime(item)
            except Exception as e:
                error_count += 1
                self.db.log_event(item["id"], "check_error", str(e))
                continue

            if not event:
                continue
            if event["kind"] == "broken":
                broken_count += 1
            elif event["kind"] == "recovered":
                recovered_count += 1
            elif event["kind"] in ("new_chapter", "new_episode"):
                new_chapter_lines.append(event["text"])

        summary_parts = []
        if broken_count or recovered_count:
            bits = []
            if broken_count:
                bits.append(f"⚠️ {broken_count} broke")
            if recovered_count:
                bits.append(f"✅ {recovered_count} recovered")
            summary_parts.append(", ".join(bits) + " — /broken for details")
        if error_count:
            summary_parts.append(f"({error_count} check error(s) — /history for details)")

        all_lines = summary_parts + new_chapter_lines
        if all_lines:
            self._notify("\n".join(all_lines))
        return all_lines

    def _check_novel(self, item):
        """Returns a structured event dict ({'kind', 'text', ...}) or None
        if nothing changed. Caller (run_check_cycle) aggregates these into
        one summary instead of pinging per item."""
        snap = None
        selector_failed = False
        already_broken = bool(item.get("broken"))

        # Tier 0: NovelFire numeric probe. Cheapest, most reliable signal -
        # checks whether chapter N+1's URL exists directly, sidestepping
        # text-matching entirely. A clean "doesn't exist yet" answer also
        # proves the site itself is reachable right now, so the common
        # steady-state "nothing new" case can skip tiers 1-2 as well, not
        # just the AI tiers.
        skip_to_tier3 = False
        current_num = item.get("last_chapter_num")
        if current_num:
            try:
                probe = novelfire.probe_next_chapter(item["url"], current_num)
            except novelfire.ProbeAmbiguous:
                probe = None  # couldn't tell - fall through to the normal pipeline
            if probe is not None:
                found, probe_snap = probe
                if found:
                    snap = probe_snap
                    skip_to_tier3 = True  # got a real answer, no need for tiers 1-2
                elif not already_broken:
                    # Confirmed: site's up, just nothing new yet. Nothing more to do.
                    return None

        # Tier 1/2: selector, then no-selector heuristic. Cheap (plain HTTP),
        # so these always run every cycle regardless of broken status - no
        # reason to throttle a free check that might heal itself for free.
        if snap is None:
            try:
                snap = fetch_snapshot(item["url"], item.get("selector"))
            except ScrapeError:
                selector_failed = True

            if selector_failed and item.get("selector"):
                try:
                    snap = fetch_snapshot(item["url"], None)
                except ScrapeError:
                    pass  # genuine failure, handled below

        # Throttle: tiers 3/4 are the expensive ones (local LLM inference,
        # external API calls). Once an item is confirmed broken, retrying
        # those every cycle burns quota for no benefit - once a day is
        # plenty until a /fix or /set actually changes something.
        retry_due = True
        if already_broken and snap is None:
            last_retry = item.get("last_broken_retry_at")
            if last_retry:
                try:
                    last_dt = datetime.datetime.fromisoformat(last_retry)
                    age_hours = (datetime.datetime.now(datetime.timezone.utc) - last_dt).total_seconds() / 3600
                    retry_due = age_hours >= self.BROKEN_RETRY_HOURS
                except ValueError:
                    retry_due = True

        used_ai_fallback = False
        used_websearch = False
        page_unreachable = False

        if snap is None and retry_due and not skip_to_tier3:
            self.db.update_item(item["id"], last_broken_retry_at=self._now_iso())

            # Tier 3: local LLM page-read. PageUnreachable means tier 3 never
            # got a chance to look (no text to read) - that's tracked
            # separately from "looked and found nothing" for a clearer
            # audit trail, but either way we fall through to tier 4.
            try:
                ai_snap = fetch_snapshot_ai(item["url"], item["title"])
                if ai_snap is not None:
                    snap = ai_snap
                    used_ai_fallback = True
            except PageUnreachable:
                page_unreachable = True

            # Tier 4: web search by title - the one tier that genuinely
            # needs live internet access, since there's no page text to
            # read (either PageUnreachable above, or tier 3 read it and
            # came up empty).
            if snap is None:
                ws_snap = fetch_snapshot_websearch(item["title"])
                if ws_snap is not None:
                    snap = ws_snap
                    used_websearch = True

        # Still no snapshot — mark or stay broken.
        if snap is None:
            if not already_broken:
                self.db.update_item(item["id"], broken=1, last_broken_retry_at=self._now_iso())
                detail = "page unreachable, tiers 3-4 attempted by title" if page_unreachable else "all tiers failed"
                self.db.log_event(item["id"], "scraper_broken", detail)
                return {"kind": "broken", "id": item["id"], "title": item["title"]}
            return None  # already known broken (or not yet due for retry) - don't spam

        chapter_num = parse_chapter_number(snap) or current_num

        # If we got here with a broken item, it healed itself.
        if already_broken:
            self.db.update_item(item["id"], broken=0)
            if used_websearch:
                recover_detail = "web search fallback found a snapshot"
            elif used_ai_fallback:
                recover_detail = "local LLM fallback found a snapshot"
            elif skip_to_tier3:
                recover_detail = "tier-0 chapter probe found a snapshot"
            else:
                recover_detail = "auto-healed"
            self.db.log_event(item["id"], "scraper_recovered", recover_detail)

            # If the selector was the problem, clear it so future checks
            # don't keep failing and falling back every cycle. Don't clear
            # it just because a fallback tier fired - that's a per-cycle
            # cost, not a one-time fix, so leave the selector in place in
            # case the next normal check works again on its own.
            if selector_failed and not used_ai_fallback and not used_websearch:
                self.db.update_item(item["id"], selector=None)
                self.db.log_event(item["id"], "selector_cleared",
                                  "old selector stopped working, cleared")

            if snap == item.get("last_snapshot"):
                return {"kind": "recovered", "id": item["id"], "title": item["title"]}
            self.db.update_item(item["id"], last_snapshot=snap, last_chapter_num=chapter_num)
            self.db.log_event(item["id"], "new_chapter", snap)
            return {"kind": "recovered", "id": item["id"], "title": item["title"]}

        if snap != item.get("last_snapshot"):
            self.db.update_item(item["id"], last_snapshot=snap, last_chapter_num=chapter_num)
            self.db.log_event(item["id"], "new_chapter", snap)
            return {"kind": "new_chapter", "id": item["id"], "title": item["title"],
                    "text": f"📖 New update for {item['title']}: {snap}"}
        return None

    def _check_anime(self, item):
        if not item.get("anilist_id"):
            return None  # no AniList ID - skip to avoid bad API requests
        state = anilist.get_anime_state(item["anilist_id"])
        if state["snapshot"] != item.get("last_snapshot"):
            self.db.update_item(item["id"], last_snapshot=state["snapshot"])
            self.db.log_event(item["id"], "new_episode", state["snapshot"])
            return {"kind": "new_episode", "id": item["id"], "title": item["title"],
                    "text": f"📺 New episode for {item['title']}: {state['snapshot']}"}
        return None

    @staticmethod
    def _now_iso():
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _notify(self, msg):
        if self.notifier:
            try:
                self.notifier(msg)
            except Exception:
                pass