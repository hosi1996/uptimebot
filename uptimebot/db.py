import sqlite3
import time
from pathlib import Path

FIELDS = {"location", "interval_min", "checks", "enabled", "only_problems", "last_run", "last_ok"}


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                interval_min INTEGER NOT NULL,
                checks TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                only_problems INTEGER NOT NULL DEFAULT 0,
                last_run REAL NOT NULL DEFAULT 0,
                last_ok INTEGER NOT NULL DEFAULT 1,
                location TEXT NOT NULL DEFAULT 'both',
                UNIQUE (chat_id, domain)
            )"""
        )
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(sites)")}
        if "location" not in columns:
            self.conn.execute("ALTER TABLE sites ADD COLUMN location TEXT NOT NULL DEFAULT 'both'")
        self.conn.commit()

    def add(self, chat_id, domain, interval_min, checks):
        """Returns the new id, or None if the domain already exists for this chat."""
        try:
            cur = self.conn.execute(
                "INSERT INTO sites (chat_id, domain, interval_min, checks, last_run) VALUES (?, ?, ?, ?, ?)",
                (chat_id, domain, interval_min, ",".join(checks), time.time()),
            )
        except sqlite3.IntegrityError:
            return None
        self.conn.commit()
        return cur.lastrowid

    def get(self, site_id, chat_id):
        return self.conn.execute(
            "SELECT * FROM sites WHERE id = ? AND chat_id = ?", (site_id, chat_id)
        ).fetchone()

    def find(self, chat_id, domain):
        return self.conn.execute(
            "SELECT * FROM sites WHERE chat_id = ? AND domain = ?", (chat_id, domain)
        ).fetchone()

    def list(self, chat_id):
        return self.conn.execute("SELECT * FROM sites WHERE chat_id = ? ORDER BY domain", (chat_id,)).fetchall()

    def due(self, now):
        return self.conn.execute(
            "SELECT * FROM sites WHERE enabled = 1 AND last_run + interval_min * 60 <= ?", (now,)
        ).fetchall()

    def update(self, site_id, **values):
        assert values.keys() <= FIELDS
        sets = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(f"UPDATE sites SET {sets} WHERE id = ?", (*values.values(), site_id))
        self.conn.commit()

    def delete(self, site_id):
        self.conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        self.conn.commit()
