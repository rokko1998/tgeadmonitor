"""SQLite-хранилище. Без ORM: схема простая, нагрузка небольшая."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from src.config.loader import Chat

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    chat_id INTEGER PRIMARY KEY,
    internal_id TEXT,
    name TEXT,
    location TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT,
    first_name TEXT,
    original_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    date TEXT,
    message_url TEXT,
    score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,           -- lead | potential | ignored | duplicate | excluded
    explanation TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages(text_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status, created_at);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    rule TEXT NOT NULL,
    category TEXT NOT NULL,
    matched_text TEXT,
    weight INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id),
    product TEXT,
    location TEXT,
    amount TEXT,
    size TEXT,
    status TEXT NOT NULL DEFAULT 'new',   -- new | confirmed | rejected
    notification_sent INTEGER NOT NULL DEFAULT 0,
    bot_message_id INTEGER,
    feedback_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT
);
"""


def _utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- sources ----------
    def upsert_sources(self, chats: Iterable[Chat]) -> None:
        with self.conn:
            for c in chats:
                if c.chat_id is None:
                    continue
                self.conn.execute(
                    """INSERT INTO sources(chat_id, internal_id, name, location, enabled, priority)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(chat_id) DO UPDATE SET internal_id=excluded.internal_id, name=excluded.name,
                       location=excluded.location, enabled=excluded.enabled, priority=excluded.priority""",
                    (c.chat_id, c.id, c.name, c.location, int(c.enabled), c.priority))

    # ---------- messages ----------
    def message_exists(self, chat_id: int, telegram_message_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM messages WHERE chat_id=? AND telegram_message_id=?",
                                (chat_id, telegram_message_id)).fetchone()
        return row is not None

    def insert_message(self, *, telegram_message_id: int, chat_id: int, user_id: int | None,
                       username: str | None, first_name: str | None, original_text: str,
                       normalized_text: str, text_hash: str, date: datetime | None,
                       message_url: str | None, score: int, status: str, explanation: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO messages(telegram_message_id, chat_id, user_id, username, first_name,
                   original_text, normalized_text, text_hash, date, message_url, score, status, explanation)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (telegram_message_id, chat_id, user_id, username, first_name, original_text,
                 normalized_text, text_hash, _utc(date) if date else None, message_url,
                 score, status, explanation))
            return int(cur.lastrowid)

    def update_message_status(self, message_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE messages SET status=? WHERE id=?", (status, message_id))

    def insert_matches(self, message_id: int, rows: Iterable[tuple[str, str, str, int]]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO matches(message_id, rule, category, matched_text, weight) VALUES(?,?,?,?,?)",
                [(message_id, rule, category, text, weight) for rule, category, text, weight in rows])

    # ---------- dedup ----------
    def find_message_by_hash(
            self,
            text_hash: str,
            window_days: int,
            reference_date: datetime | None = None,
    ) -> int | None:
        ref = reference_date or datetime.now(timezone.utc)
        since = _utc(ref - timedelta(days=window_days))
        until = _utc(ref)

        row = self.conn.execute(
            """
            SELECT id
            FROM messages
            WHERE text_hash=?
              AND date>=?
              AND date<=?
            ORDER BY date DESC
            LIMIT 1
            """,
            (text_hash, since, until),
        ).fetchone()

        return int(row["id"]) if row else None

    def recent_lead_texts(
            self,
            window_days: int,
            limit: int,
            reference_date: datetime | None = None,
    ) -> list[tuple[int, int | None, str]]:
        ref = reference_date or datetime.now(timezone.utc)
        since = _utc(ref - timedelta(days=window_days))
        until = _utc(ref)

        rows = self.conn.execute(
            """
            SELECT id, user_id, normalized_text
            FROM messages
            WHERE status IN ('lead', 'duplicate')
              AND date>=?
              AND date<=?
            ORDER BY date DESC
            LIMIT ?
            """,
            (since, until, limit),
        ).fetchall()

        return [
            (int(r["id"]), r["user_id"], r["normalized_text"])
            for r in rows
        ]

    # ---------- leads ----------
    def insert_lead(self, message_id: int, product: str | None, location: str | None,
                    amount: str | None, size: str | None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO leads(message_id, product, location, amount, size) VALUES(?,?,?,?,?)",
                (message_id, product, location, amount, size))
            return int(cur.lastrowid)

    def set_lead_notified(self, lead_id: int, bot_message_id: int | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE leads SET notification_sent=1, bot_message_id=? WHERE id=?",
                              (bot_message_id, lead_id))

    def set_feedback(self, lead_id: int, status: str) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE leads SET status=?, feedback_at=? WHERE id=?",
                                    (status, _utc(), lead_id))
            return cur.rowcount > 0

    # ---------- notifications ----------
    def enqueue_notification(self, lead_id: int, payload: dict[str, Any]) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO notifications(lead_id, payload) VALUES(?,?)",
                                    (lead_id, json.dumps(payload, ensure_ascii=False)))
            return int(cur.lastrowid)

    def pending_notifications(self, max_attempts: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notifications WHERE status='pending' AND attempts<? ORDER BY id LIMIT 20",
            (max_attempts,)).fetchall()

    def mark_notification_sent(self, notification_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE notifications SET status='sent', sent_at=? WHERE id=?",
                              (_utc(), notification_id))

    def mark_notification_failed(self, notification_id: int, error: str, final: bool = False) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE notifications SET attempts=attempts+1, last_error=?, status=? WHERE id=?",
                (error[:500], "failed" if final else "pending", notification_id))

    # ---------- stats ----------
    def stats(self) -> dict[str, Any]:
        q = self.conn.execute
        out: dict[str, Any] = {}
        for status, cnt in q("SELECT status, COUNT(*) FROM messages GROUP BY status").fetchall():
            out[f"messages_{status}"] = cnt
        out["messages_total"] = q("SELECT COUNT(*) FROM messages").fetchone()[0]
        out["leads_total"] = q("SELECT COUNT(*) FROM leads").fetchone()[0]
        out["notifications_sent"] = q("SELECT COUNT(*) FROM notifications WHERE status='sent'").fetchone()[0]
        out["notifications_pending"] = q("SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0]
        out["true_positive"] = q("SELECT COUNT(*) FROM leads WHERE status='confirmed'").fetchone()[0]
        out["false_positive"] = q("SELECT COUNT(*) FROM leads WHERE status='rejected'").fetchone()[0]
        rated = out["true_positive"] + out["false_positive"]
        out["precision"] = round(out["true_positive"] / rated, 3) if rated else None
        out["leads_per_chat"] = {r[0]: r[1] for r in q(
            """SELECT COALESCE(s.name, m.chat_id), COUNT(*) FROM leads l
               JOIN messages m ON m.id=l.message_id LEFT JOIN sources s ON s.chat_id=m.chat_id
               GROUP BY 1 ORDER BY 2 DESC LIMIT 20""").fetchall()}
        out["leads_per_product"] = {r[0]: r[1] for r in q(
            "SELECT COALESCE(product,'-'), COUNT(*) FROM leads GROUP BY 1 ORDER BY 2 DESC LIMIT 20").fetchall()}
        out["leads_per_location"] = {r[0]: r[1] for r in q(
            "SELECT COALESCE(location,'-'), COUNT(*) FROM leads GROUP BY 1 ORDER BY 2 DESC LIMIT 20").fetchall()}
        return out
