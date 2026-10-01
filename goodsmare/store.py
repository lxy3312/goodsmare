"""SQLite：见过哪些商品、推送过哪些、每个关注在每个站的最近状态，还有推送页。"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    watch_id TEXT, source TEXT, item_id TEXT, price INTEGER,
    first_seen INTEGER, last_seen INTEGER,
    PRIMARY KEY (watch_id, source, item_id)
);
CREATE TABLE IF NOT EXISTS baseline (
    watch_id TEXT, source TEXT, sig TEXT, done_at INTEGER,
    PRIMARY KEY (watch_id, source)
);
CREATE TABLE IF NOT EXISTS feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, item_id TEXT, title TEXT, price INTEGER, old_price INTEGER,
    url TEXT, image TEXT, extra TEXT, watch_id TEXT, watch_name TEXT,
    kind TEXT, found_at INTEGER
);
CREATE INDEX IF NOT EXISTS feed_item ON feed (source, item_id);
CREATE TABLE IF NOT EXISTS status (
    watch_id TEXT, source TEXT, checked_at INTEGER, ok INTEGER, count INTEGER, message TEXT,
    PRIMARY KEY (watch_id, source)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS page (id TEXT PRIMARY KEY, created INTEGER, data TEXT);
"""

FEED_KEEP = 3000
SEEN_KEEP_DAYS = 45
PAGE_KEEP_DAYS = 30


class Store:
    def __init__(self, path: Path | str):
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            self.db.commit()

    # —— 基线：某个关注在某个站第一次扫描时，只记下现有商品，不推送 ——
    def baseline_sig(self, watch_id: str, source: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT sig FROM baseline WHERE watch_id=? AND source=?",
                                  (watch_id, source)).fetchone()
        return row["sig"] if row else None

    def set_baseline(self, watch_id: str, source: str, sig: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO baseline VALUES (?,?,?,?)",
                            (watch_id, source, sig, int(time.time())))
            self.db.commit()

    def seen_prices(self, watch_id: str, source: str, ids: list[str]) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        with self._lock:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                q = ("SELECT item_id, price FROM seen WHERE watch_id=? AND source=? AND item_id IN (%s)"
                     % ",".join("?" * len(chunk)))
                for row in self.db.execute(q, (watch_id, source, *chunk)):
                    out[row["item_id"]] = row["price"]
        return out

    def mark_seen(self, watch_id: str, source: str, items) -> None:
        now = int(time.time())
        with self._lock:
            self.db.executemany(
                "INSERT INTO seen VALUES (?,?,?,?,?,?) ON CONFLICT(watch_id, source, item_id) "
                "DO UPDATE SET price=excluded.price, last_seen=excluded.last_seen",
                [(watch_id, source, it.id, it.price, now, now) for it in items])
            self.db.commit()

    # —— 推送流 ——
    def already_pushed(self, source: str, item_id: str, kind: str, price: int | None) -> bool:
        with self._lock:
            if kind == "new":
                row = self.db.execute("SELECT 1 FROM feed WHERE source=? AND item_id=? LIMIT 1",
                                      (source, item_id)).fetchone()
            else:
                row = self.db.execute(
                    "SELECT 1 FROM feed WHERE source=? AND item_id=? AND kind='drop' AND price IS ? LIMIT 1",
                    (source, item_id, price)).fetchone()
        return row is not None

    def add_feed(self, hit) -> int:
        it = hit.item
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO feed (source, item_id, title, price, old_price, url, image, extra, "
                "watch_id, watch_name, kind, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (it.source, it.id, it.title, it.price, hit.old_price, it.url, it.image, it.extra,
                 hit.watch_id, hit.watch_name, hit.kind, int(time.time())))
            self.db.commit()
            return cur.lastrowid

    def feed(self, after: int = 0, before: int = 0, limit: int = 60,
             watch_id: str = "", source: str = "") -> list[dict]:
        where, args = [], []
        if after:
            where.append("id > ?")
            args.append(after)
        if before:
            where.append("id < ?")
            args.append(before)
        if watch_id:
            where.append("watch_id = ?")
            args.append(watch_id)
        if source:
            where.append("source = ?")
            args.append(source)
        sql = "SELECT * FROM feed"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def clear_feed(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM feed")
            self.db.commit()

    # —— 状态 ——
    def set_status(self, watch_id: str, source: str, ok: bool, count: int, message: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO status VALUES (?,?,?,?,?,?)",
                            (watch_id, source, int(time.time()), int(ok), count, message))
            self.db.commit()

    def statuses(self) -> dict:
        out: dict = {}
        with self._lock:
            for r in self.db.execute("SELECT * FROM status"):
                out.setdefault(r["watch_id"], {})[r["source"]] = {
                    "checked_at": r["checked_at"], "ok": bool(r["ok"]),
                    "count": r["count"], "message": r["message"]}
        return out

    def forget_watch(self, watch_id: str) -> None:
        with self._lock:
            for table in ("seen", "baseline", "status"):
                self.db.execute(f"DELETE FROM {table} WHERE watch_id=?", (watch_id,))
            self.db.commit()

    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
            self.db.commit()

    # —— 推送页：一轮推送一页，链接里的随机 ID 就是钥匙 ——
    def add_page(self, data: dict) -> str:
        pid = secrets.token_urlsafe(12)
        with self._lock:
            self.db.execute("INSERT INTO page VALUES (?,?,?)",
                            (pid, int(time.time()), json.dumps(data, ensure_ascii=False)))
            self.db.commit()
        return pid

    def page(self, pid: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT data FROM page WHERE id=?", (pid,)).fetchone()
        return json.loads(row["data"]) if row else None

    def prune(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM seen WHERE last_seen < ?",
                            (int(time.time()) - SEEN_KEEP_DAYS * 86400,))
            self.db.execute("DELETE FROM page WHERE created < ?",
                            (int(time.time()) - PAGE_KEEP_DAYS * 86400,))
            self.db.execute("DELETE FROM feed WHERE id <= (SELECT id FROM feed ORDER BY id DESC "
                            "LIMIT 1 OFFSET ?)", (FEED_KEEP,))
            self.db.commit()
