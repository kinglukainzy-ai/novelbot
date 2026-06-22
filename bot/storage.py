"""
storage.py - all database access lives here.
Single SQLite file, no server needed. Safe for one Oracle Free VM.
"""
import sqlite3
import os
import datetime
import threading

_lock = threading.Lock()


class Storage:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,              -- 'novel' or 'anime'
                    title TEXT NOT NULL,
                    url TEXT,                         -- novel source page (novels only)
                    selector TEXT,                    -- CSS selector for novels (optional)
                    anilist_id INTEGER,                -- anime only
                    status TEXT DEFAULT 'reading',    -- reading/watching/on_hold/completed/dropped
                    rating REAL,
                    notes TEXT,
                    tags TEXT,                        -- comma-separated
                    last_snapshot TEXT,                -- last seen chapter/episode marker
                    last_checked TEXT,
                    broken INTEGER DEFAULT 0,         -- 1 if scraper is failing
                    progress_current INTEGER,         -- chapter/episode you've reached
                    progress_total INTEGER,           -- total chapters/episodes known
                    last_chapter_title TEXT,           -- title of last-read chapter (seed data)
                    last_read_at TEXT,                 -- when you actually last read/watched it
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            self._migrate_add_columns(c)
            c.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    event TEXT NOT NULL,              -- e.g. 'new_chapter', 'marked_completed'
                    detail TEXT,
                    created_at TEXT,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                )
            """)
            c.commit()

    def _migrate_add_columns(self, c):
        """Adds new columns to an existing DB created before this version, without
        breaking it. SQLite has no 'ADD COLUMN IF NOT EXISTS', so we check first."""
        existing = {row[1] for row in c.execute("PRAGMA table_info(items)").fetchall()}
        new_cols = {
            "progress_current": "INTEGER",
            "progress_total": "INTEGER",
            "last_chapter_title": "TEXT",
            "last_read_at": "TEXT",
        }
        for col, coltype in new_cols.items():
            if col not in existing:
                c.execute(f"ALTER TABLE items ADD COLUMN {col} {coltype}")
        c.commit()

    @staticmethod
    def _now():
        return datetime.datetime.utcnow().isoformat()

    # ---------- items ----------

    def find_by_title(self, title):
        """Case-insensitive exact title match - used by the seed script and
        duplicate detection on /add."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM items WHERE LOWER(title)=LOWER(?)", (title,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_seed_item(self, type_, title, url=None, status="reading",
                          progress_current=None, progress_total=None,
                          last_chapter_title=None, last_read_at=None):
        """Insert a title from imported reading history, or update its progress
        if it's already in the library (re-running the seed script is safe)."""
        existing = self.find_by_title(title)
        if existing:
            self.update_item(
                existing["id"],
                progress_current=progress_current,
                progress_total=progress_total,
                last_chapter_title=last_chapter_title,
                last_read_at=last_read_at,
                status=status,
            )
            return existing["id"], False
        with _lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO items
                   (type, title, url, status, progress_current, progress_total,
                    last_chapter_title, last_read_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (type_, title, url, status, progress_current, progress_total,
                 last_chapter_title, last_read_at, self._now(), self._now()),
            )
            c.commit()
            return cur.lastrowid, True

    def add_item(self, type_, title, url=None, selector=None, anilist_id=None, status="reading"):
        with _lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO items (type, title, url, selector, anilist_id, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (type_, title, url, selector, anilist_id, status, self._now(), self._now()),
            )
            c.commit()
            return cur.lastrowid

    def get_item(self, item_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def list_items(self, type_=None, status=None):
        q = "SELECT * FROM items WHERE 1=1"
        params = []
        if type_:
            q += " AND type=?"
            params.append(type_)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY id DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    def update_item(self, item_id, **fields):
        if not fields:
            return
        fields["updated_at"] = self._now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with _lock, self._conn() as c:
            c.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))
            c.commit()

    def delete_item(self, item_id):
        with _lock, self._conn() as c:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
            c.execute("DELETE FROM history WHERE item_id=?", (item_id,))
            c.commit()

    def all_active_items(self):
        """Items worth checking for updates (not dropped/completed)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE status NOT IN ('dropped','completed')"
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- history ----------

    def log_event(self, item_id, event, detail=None):
        with _lock, self._conn() as c:
            c.execute(
                "INSERT INTO history (item_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                (item_id, event, detail, self._now()),
            )
            c.commit()

    def recent_history(self, limit=20):
        with self._conn() as c:
            rows = c.execute(
                """SELECT history.*, items.title, items.type FROM history
                   JOIN items ON items.id = history.item_id
                   ORDER BY history.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self):
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            by_status = c.execute(
                "SELECT status, COUNT(*) as n FROM items GROUP BY status"
            ).fetchall()
            by_type = c.execute(
                "SELECT type, COUNT(*) as n FROM items GROUP BY type"
            ).fetchall()
            return {
                "total": total,
                "by_status": {r["status"]: r["n"] for r in by_status},
                "by_type": {r["type"]: r["n"] for r in by_type},
            }
