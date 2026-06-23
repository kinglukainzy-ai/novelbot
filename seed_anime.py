"""
seed_anime.py - one-time import of a known anime watch history into bot.db.

All titles below are seeded as 'completed'. Safe to re-run: existing entries
are matched by title and skipped rather than duplicated.

Usage:
    python seed_anime.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))
from bot.storage import Storage
from bot import anilist as al

DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")

# Titles to seed — all marked completed.
TITLES = [
    "Misfits of the Demon King Academy",
    "Daily Life of the Immortal King",
    "High School DxD",
    "That Time I Got Reincarnated as a Slime",
    "Re:Monster",
    "Full Time Magister",
    "Alchemy of Souls",
    "Fullmetal Alchemist: Brotherhood",
    "Demon Slayer",
    "Seven Deadly Sins",
    "Blue Lock",
    "Noble Reincarnation",
    "The World's Finest Assassin Gets Reincarnated in Another World as an Aristocrat",
    "Black Summoner",
    "Bleach",
    "Dr. Stone",
    "Black Clover",
    "Tokyo Ghoul",
    "Jujutsu Kaisen",
    "Hunter x Hunter",
    "Re:Zero",
    "Death Note",
    "Kengan Ashura",
    "The Eminence in Shadow",
    "Kaiju No. 8",
    "The Rising of the Shield Hero",
]


def main():
    store = Storage(DB_PATH)
    added, skipped, failed = 0, 0, 0

    for title in TITLES:
        # Skip if already in library (case-insensitive title match)
        existing = store.find_by_title(title)
        if existing:
            print(f"  SKIP  {title!r} (already #{existing['id']})")
            skipped += 1
            continue

        # Search AniList for best match
        try:
            results = al.search_anime(title, limit=3)
        except Exception as e:
            print(f"  FAIL  {title!r} — AniList error: {e}")
            failed += 1
            time.sleep(1)
            continue

        if not results:
            print(f"  FAIL  {title!r} — not found on AniList")
            failed += 1
            continue

        best = results[0]
        item_id = store.add_item(
            type_="anime",
            title=best["title"] or title,
            url=f"https://anilist.co/anime/{best['id']}",
            anilist_id=best["id"],
            status="completed",
        )
        print(f"  ADD   #{item_id} {best['title']!r}  (AniList id {best['id']})")
        added += 1

        # Be polite to AniList's free API — avoid rate limiting
        time.sleep(0.5)

    print()
    print(f"Seed complete: {added} added, {skipped} already existed, {failed} failed.")
    print(f"Database: {os.path.abspath(DB_PATH)}")


if __name__ == "__main__":
    main()
