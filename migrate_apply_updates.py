"""
migrate_apply_updates.py - one-time migration script to apply recent logic changes
to existing database records.

What it does:
  1. Backfill timezone-aware timestamps  — old records stored naive UTC strings;
     this appends "+00:00" to every timestamp column that's missing it.
  2. Look up NovelFire URLs             — novels with no URL get a NovelFire
     search; the best match URL is stored.
  3. Validate / fix existing URLs       — for each novel that has a URL, try to
     fetch the latest chapter snapshot.  If it succeeds, clear the broken flag
     and update last_snapshot.  If a NovelFire URL 404s, try a title search
     to find the correct current URL.
  4. Log everything                     — prints a summary so you can review
     what changed before running the bot.

Usage:
    python migrate_apply_updates.py          # default: data/bot.db
    DATABASE_PATH=mydata/bot.db python migrate_apply_updates.py

Safe to re-run: it skips records that are already up-to-date.
"""
import os
import sys
import time
import sqlite3
import datetime

sys.path.insert(0, os.path.dirname(__file__))

from bot.storage import Storage
from bot.scraper import fetch_snapshot, ScrapeError
from bot import novelfire

DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")

# Columns that contain ISO-format timestamps
TIMESTAMP_COLS = ["created_at", "updated_at", "last_checked", "last_read_at"]
HISTORY_TIMESTAMP_COLS = ["created_at"]

# Polite delay between HTTP requests to avoid hammering NovelFire
REQUEST_DELAY = 1.5  # seconds


def _needs_tz(value: str | None) -> bool:
    """Return True if the timestamp string exists but lacks timezone info."""
    if not value:
        return False
    # Already has timezone offset (e.g. "+00:00", "+05:30", "Z")
    if value.endswith("Z") or "+" in value[10:] or value.endswith("+00:00"):
        return False
    return True


def backfill_timestamps(db_path: str) -> dict:
    """Append '+00:00' to all naive-UTC timestamp strings in items and history."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stats = {"items_fixed": 0, "history_fixed": 0}

    # --- items table ---
    rows = conn.execute("SELECT * FROM items").fetchall()
    for row in rows:
        updates = {}
        for col in TIMESTAMP_COLS:
            val = row[col] if col in row.keys() else None
            if _needs_tz(val):
                updates[col] = val + "+00:00"
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE items SET {set_clause} WHERE id=?",
                (*updates.values(), row["id"]),
            )
            stats["items_fixed"] += 1

    # --- history table ---
    rows = conn.execute("SELECT * FROM history").fetchall()
    for row in rows:
        updates = {}
        for col in HISTORY_TIMESTAMP_COLS:
            val = row[col] if col in row.keys() else None
            if _needs_tz(val):
                updates[col] = val + "+00:00"
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE history SET {set_clause} WHERE id=?",
                (*updates.values(), row["id"]),
            )
            stats["history_fixed"] += 1

    conn.commit()
    conn.close()
    return stats


def fix_novel_urls_and_broken(db_path: str) -> dict:
    """
    For each novel in the library:
      - If it has no URL → search NovelFire by title and set the URL.
      - If it has a URL  → try fetching the latest chapter snapshot.
        • On success: clear broken flag, update last_snapshot.
        • On failure with a NovelFire URL: try a title search to find
          the correct new URL, then retry.
    """
    store = Storage(db_path)
    stats = {
        "urls_added": 0,
        "urls_corrected": 0,
        "broken_cleared": 0,
        "snapshots_updated": 0,
        "still_broken": 0,
        "skipped_anime": 0,
    }

    items = store.list_items()
    total = len(items)

    for i, item in enumerate(items, 1):
        item_id = item["id"]
        title = item["title"]
        item_type = item["type"]

        print(f"  [{i}/{total}] #{item_id} {title} ", end="", flush=True)

        if item_type != "novel":
            print("(anime — skip)")
            stats["skipped_anime"] += 1
            continue

        url = item.get("url")

        # --- Step A: no URL → search NovelFire ---
        if not url:
            print("→ no URL, searching NovelFire... ", end="", flush=True)
            time.sleep(REQUEST_DELAY)
            results = novelfire.search_novel(title)
            if results:
                url = results[0]["url"]
                store.update_item(item_id, url=url)
                store.log_event(item_id, "url_added", f"migration: {url}")
                stats["urls_added"] += 1
                print(f"found: {url}")
            else:
                print("NOT FOUND — still no URL")
                stats["still_broken"] += 1
                continue

        # --- Step B: validate the URL by fetching latest chapter ---
        snap = None
        try:
            time.sleep(REQUEST_DELAY)
            snap = fetch_snapshot(url, item.get("selector"))
        except ScrapeError:
            pass

        # If that failed and the URL is a NovelFire URL, try searching
        # for the title again — the slug may have changed.
        if snap is None and "novelfire.net" in (url or ""):
            print("→ URL broken, re-searching NovelFire... ", end="", flush=True)
            time.sleep(REQUEST_DELAY)
            results = novelfire.search_novel(title)
            if results:
                new_url = results[0]["url"]
                if new_url != url:
                    store.update_item(item_id, url=new_url, selector=None)
                    store.log_event(
                        item_id, "url_corrected",
                        f"migration: {url} → {new_url}",
                    )
                    url = new_url
                    stats["urls_corrected"] += 1
                    # Retry with the corrected URL
                    try:
                        time.sleep(REQUEST_DELAY)
                        snap = fetch_snapshot(url, None)
                    except ScrapeError:
                        pass

        # Also try the NovelFire-specific latest_chapter_snapshot helper
        # as a fallback — it uses different selectors than the generic scraper.
        if snap is None and "novelfire.net" in (url or ""):
            time.sleep(REQUEST_DELAY)
            snap = novelfire.latest_chapter_snapshot(url)

        # --- Step C: apply results ---
        if snap is not None:
            updates = {"broken": 0, "last_snapshot": snap}
            if item.get("broken"):
                stats["broken_cleared"] += 1
            if snap != item.get("last_snapshot"):
                stats["snapshots_updated"] += 1
            store.update_item(item_id, **updates)
            print(f"✓ latest: {snap[:60]}{'…' if len(snap) > 60 else ''}")
        else:
            if not item.get("broken"):
                store.update_item(item_id, broken=1)
                store.log_event(item_id, "scraper_broken", "migration: could not validate")
            stats["still_broken"] += 1
            print("✗ still broken")

    return stats


def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {os.path.abspath(DB_PATH)}")
        print("Run seed_library.py first, or set DATABASE_PATH.")
        sys.exit(1)

    print(f"Database: {os.path.abspath(DB_PATH)}")
    print()

    # ── Phase 1: Timestamps ──────────────────────────────────────────
    print("Phase 1: Backfilling timezone-aware timestamps...")
    ts_stats = backfill_timestamps(DB_PATH)
    print(f"  Items fixed:   {ts_stats['items_fixed']}")
    print(f"  History fixed: {ts_stats['history_fixed']}")
    print()

    # ── Phase 2: URLs & broken flags ─────────────────────────────────
    print("Phase 2: Fixing URLs and validating scrapers...")
    print("  (this makes HTTP requests — it may take a few minutes)")
    print()
    url_stats = fix_novel_urls_and_broken(DB_PATH)
    print()

    # ── Summary ──────────────────────────────────────────────────────
    print("=" * 60)
    print("Migration complete!")
    print()
    print("Timestamps:")
    print(f"  Items updated:    {ts_stats['items_fixed']}")
    print(f"  History updated:  {ts_stats['history_fixed']}")
    print()
    print("URLs & scrapers:")
    print(f"  URLs added:       {url_stats['urls_added']}")
    print(f"  URLs corrected:   {url_stats['urls_corrected']}")
    print(f"  Broken cleared:   {url_stats['broken_cleared']}")
    print(f"  Snapshots updated:{url_stats['snapshots_updated']}")
    print(f"  Still broken:     {url_stats['still_broken']}")
    print(f"  Anime skipped:    {url_stats['skipped_anime']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
