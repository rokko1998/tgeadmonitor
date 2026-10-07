"""SQLite storage."""
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
    username TEXT,
    name TEXT,
    location TEXT,
    source_type TEXT NOT NULL DEFAULT 'legacy',
    require_location INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 1,
    added_via TEXT NOT NULL DEFAULT 'config'
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
    status TEXT NOT NULL,
    explanation TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages(text_hash, date);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status, date);
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
    lead_type TEXT NOT NULL DEFAULT 'lead',
    source_type TEXT NOT NULL DEFAULT 'legacy',
    product TEXT,
    location TEXT,
    amount TEXT,
    size TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    notification_sent INTEGER NOT NULL DEFAULT 0,
    bot_message_id INTEGER,
    feedback_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
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
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _columns(self, table: str) -> set[str]:
        return {str(r["name"]) for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _add_column(self, table: str, definition: str, name: str) -> None:
        if name not in self._columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate(self) -> None:
        # Additive migration so an existing Dokploy volume can be reused safely.
        with self.conn:
            self._add_column("sources", "username TEXT", "username")
            self._add_column("sources", "source_type TEXT NOT NULL DEFAULT 'legacy'", "source_type")
            self._add_column("sources", "require_location INTEGER NOT NULL DEFAULT 0", "require_location")
            self._add_column("sources", "added_via TEXT NOT NULL DEFAULT 'config'", "added_via")
            self._add_column("leads", "lead_type TEXT NOT NULL DEFAULT 'lead'", "lead_type")
            self._add_column("leads", "source_type TEXT NOT NULL DEFAULT 'legacy'", "source_type")

    # ---------- sources ----------
    def upsert_source(self, c: Chat, added_via: str = "config") -> None:
        if c.chat_id is None:
            return
        with self.conn:
            self.conn.execute(
                """INSERT INTO sources(
                       chat_id, internal_id, username, name, location, source_type,
                       require_location, enabled, priority, added_via)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       internal_id=excluded.internal_id,
                       username=excluded.username,
                       name=excluded.name,
                       location=COALESCE(excluded.location, sources.location),
                       source_type=excluded.source_type,
                       require_location=excluded.require_location,
                       enabled=excluded.enabled,
                       priority=excluded.priority""",
                (c.chat_id, c.id, c.username, c.name, c.location, c.source_type,
                 int(c.require_location), int(c.enabled), c.priority, added_via),
            )

    def upsert_sources(self, chats: Iterable[Chat], added_via: str = "config") -> None:
        for c in chats:
            self.upsert_source(c, added_via)

    def runtime_sources(self) -> list[Chat]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE enabled=1 AND added_via='telegram_bot' ORDER BY priority DESC, name"
        ).fetchall()
        return [
            Chat(
                id=r["internal_id"], name=r["name"], chat_id=int(r["chat_id"]),
                username=r["username"], location=r["location"],
                source_type=r["source_type"] or "unclassified",
                require_location=bool(r["require_location"]), enabled=True,
                priority=int(r["priority"]), comment="runtime source",
            )
            for r in rows
        ]

    # ---------- messages ----------
    def message_exists(self, chat_id: int, telegram_message_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=? AND telegram_message_id=?",
            (chat_id, telegram_message_id),
        ).fetchone() is not None

    def insert_message(self, *, telegram_message_id: int, chat_id: int, user_id: int | None,
                       username: str | None, first_name: str | None, original_text: str,
                       normalized_text: str, text_hash: str, date: datetime | None,
                       message_url: str | None, score: int, status: str, explanation: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO messages(
                       telegram_message_id, chat_id, user_id, username, first_name,
                       original_text, normalized_text, text_hash, date, message_url,
                       score, status, explanation)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (telegram_message_id, chat_id, user_id, username, first_name, original_text,
                 normalized_text, text_hash, _utc(date) if date else None, message_url,
                 score, status, explanation),
            )
            return int(cur.lastrowid)

    def update_message_status(self, message_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE messages SET status=? WHERE id=?", (status, message_id))

    def insert_matches(self, message_id: int, rows: Iterable[tuple[str, str, str, int]]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO matches(message_id, rule, category, matched_text, weight) VALUES(?,?,?,?,?)",
                [(message_id, rule, category, text, weight) for rule, category, text, weight in rows],
            )

    # ---------- dedup ----------
    def find_message_by_hash(self, text_hash: str, window_days: int,
                             reference_date: datetime | None = None) -> int | None:
        ref = reference_date or datetime.now(timezone.utc)
        since, until = _utc(ref - timedelta(days=window_days)), _utc(ref)
        row = self.conn.execute(
            """SELECT id FROM messages
               WHERE text_hash=? AND date>=? AND date<=?
               ORDER BY date DESC LIMIT 1""",
            (text_hash, since, until),
        ).fetchone()
        return int(row["id"]) if row else None

    def recent_lead_texts(self, window_days: int, limit: int,
                          reference_date: datetime | None = None) -> list[tuple[int, int | None, str]]:
        ref = reference_date or datetime.now(timezone.utc)
        since, until = _utc(ref - timedelta(days=window_days)), _utc(ref)
        rows = self.conn.execute(
            """SELECT id, user_id, normalized_text FROM messages
               WHERE status IN ('lead','project_signal','duplicate')
                 AND date>=? AND date<=?
               ORDER BY date DESC LIMIT ?""",
            (since, until, limit),
        ).fetchall()
        return [(int(r["id"]), r["user_id"], r["normalized_text"]) for r in rows]

    # ---------- opportunities ----------
    def insert_lead(self, message_id: int, lead_type: str, source_type: str,
                    product: str | None, location: str | None,
                    amount: str | None, size: str | None) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO leads(
                       message_id, lead_type, source_type, product, location, amount, size)
                   VALUES(?,?,?,?,?,?,?)""",
                (message_id, lead_type, source_type, product, location, amount, size),
            )
            return int(cur.lastrowid)

    def set_lead_notified(self, lead_id: int, bot_message_id: int | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE leads SET notification_sent=1, bot_message_id=? WHERE id=?",
                (bot_message_id, lead_id),
            )

    def set_feedback(self, lead_id: int, status: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE leads SET status=?, feedback_at=? WHERE id=?",
                (status, _utc(), lead_id),
            )
            return cur.rowcount > 0

    # ---------- notifications ----------
    def enqueue_notification(self, lead_id: int, payload: dict[str, Any]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO notifications(lead_id, payload) VALUES(?,?)",
                (lead_id, json.dumps(payload, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def pending_notifications(self, max_attempts: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notifications WHERE status='pending' AND attempts<? ORDER BY id LIMIT 20",
            (max_attempts,),
        ).fetchall()

    def mark_notification_sent(self, notification_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE notifications SET status='sent', sent_at=? WHERE id=?",
                (_utc(), notification_id),
            )

    def mark_notification_failed(self, notification_id: int, error: str, final: bool = False) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE notifications SET attempts=attempts+1, last_error=?, status=? WHERE id=?",
                (error[:500], "failed" if final else "pending", notification_id),
            )

    # ---------- analytics ----------
    def stats(self, days: int | None = None) -> dict[str, Any]:
        where = ""
        params: tuple[Any, ...] = ()
        if days is not None:
            where = "WHERE COALESCE(m.date,m.created_at) >= datetime('now', ?)"
            params = (f"-{int(days)} days",)

        q = self.conn.execute
        out: dict[str, Any] = {"window_days": days}

        out["messages_total"] = q(
            f"SELECT COUNT(*) FROM messages m {where}", params
        ).fetchone()[0]

        lead_where = where.replace("WHERE", "WHERE", 1)
        out["opportunities_total"] = q(
            f"""SELECT COUNT(*) FROM leads l
                JOIN messages m ON m.id=l.message_id {lead_where}""", params
        ).fetchone()[0]
        out["direct_leads"] = q(
            f"""SELECT COUNT(*) FROM leads l
                JOIN messages m ON m.id=l.message_id
                {lead_where + (' AND' if lead_where else ' WHERE')} l.lead_type='lead'""", params
        ).fetchone()[0]
        out["project_signals"] = q(
            f"""SELECT COUNT(*) FROM leads l
                JOIN messages m ON m.id=l.message_id
                {lead_where + (' AND' if lead_where else ' WHERE')} l.lead_type='project_signal'""", params
        ).fetchone()[0]
        out["confirmed"] = q(
            f"""SELECT COUNT(*) FROM leads l
                JOIN messages m ON m.id=l.message_id
                {lead_where + (' AND' if lead_where else ' WHERE')} l.status='confirmed'""", params
        ).fetchone()[0]
        out["rejected"] = q(
            f"""SELECT COUNT(*) FROM leads l
                JOIN messages m ON m.id=l.message_id
                {lead_where + (' AND' if lead_where else ' WHERE')} l.status='rejected'""", params
        ).fetchone()[0]
        rated = out["confirmed"] + out["rejected"]
        out["precision"] = round(out["confirmed"] / rated, 3) if rated else None

        perf_params = params + params
        time_msg = "AND COALESCE(m.date,m.created_at) >= datetime('now', ?)" if days is not None else ""
        rows = q(
            f"""SELECT
                    s.name,
                    s.source_type,
                    COUNT(DISTINCT m.id) AS messages,
                    COUNT(DISTINCT l.id) AS opportunities,
                    COUNT(DISTINCT CASE WHEN l.status='confirmed' THEN l.id END) AS confirmed,
                    COUNT(DISTINCT CASE WHEN l.status='rejected' THEN l.id END) AS rejected
                FROM sources s
                LEFT JOIN messages m ON m.chat_id=s.chat_id {time_msg}
                LEFT JOIN leads l ON l.message_id=m.id
                WHERE s.enabled=1
                GROUP BY s.chat_id, s.name, s.source_type
                ORDER BY confirmed DESC, opportunities DESC, messages DESC""",
            params,
        ).fetchall()

        performance = []
        for r in rows:
            messages = int(r["messages"])
            opportunities = int(r["opportunities"])
            confirmed = int(r["confirmed"])
            rejected = int(r["rejected"])
            rated_source = confirmed + rejected
            performance.append({
                "source": r["name"],
                "source_type": r["source_type"],
                "messages": messages,
                "opportunities": opportunities,
                "confirmed": confirmed,
                "rejected": rejected,
                "opportunities_per_1000": round(opportunities * 1000 / messages, 2) if messages else 0,
                "confirmed_per_1000": round(confirmed * 1000 / messages, 2) if messages else 0,
                "precision": round(confirmed / rated_source, 3) if rated_source else None,
            })
        out["source_performance"] = performance[:30]

        type_rows = q(
            f"""SELECT
                    COALESCE(s.source_type,'legacy') AS source_type,
                    COUNT(DISTINCT m.id) AS messages,
                    COUNT(DISTINCT l.id) AS opportunities,
                    COUNT(DISTINCT CASE WHEN l.status='confirmed' THEN l.id END) AS confirmed
                FROM messages m
                LEFT JOIN sources s ON s.chat_id=m.chat_id
                LEFT JOIN leads l ON l.message_id=m.id
                {where}
                GROUP BY 1 ORDER BY confirmed DESC, opportunities DESC""",
            params,
        ).fetchall()
        out["source_type_performance"] = [
            {
                "source_type": r["source_type"],
                "messages": int(r["messages"]),
                "opportunities": int(r["opportunities"]),
                "confirmed": int(r["confirmed"]),
                "confirmed_per_1000": round(int(r["confirmed"]) * 1000 / int(r["messages"]), 2)
                if int(r["messages"]) else 0,
            }
            for r in type_rows
        ]
        return out
