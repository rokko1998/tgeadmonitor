"""SQLite storage."""
from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from src.config.loader import Chat
from src.storage.semantic import SemanticStorage

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
CREATE TABLE IF NOT EXISTS source_requests (
    link_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    chat_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    notify INTEGER NOT NULL DEFAULT 1,
    approval_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

def _utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")

class Database(SemanticStorage):
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.init_semantic_storage()

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
            self._add_column("sources", "analysis_enabled INTEGER NOT NULL DEFAULT 1", "analysis_enabled")
            self._add_column("sources", "pause_reason TEXT", "pause_reason")
            self._add_column("sources", "paused_at TEXT", "paused_at")
            self._add_column("sources", "pinned INTEGER NOT NULL DEFAULT 0", "pinned")
            self._add_column("sources", "quality_since TEXT", "quality_since")
            self._add_column("sources", "observed_seconds INTEGER NOT NULL DEFAULT 0", "observed_seconds")
            self.conn.execute("UPDATE sources SET quality_since=datetime('now') WHERE quality_since IS NULL")

    # ---------- sources ----------
    def upsert_source(self, c: Chat, added_via: str = "config") -> None:
        if c.chat_id is None:
            return
        with self.conn:
            self.conn.execute(
                """INSERT INTO sources(
                       chat_id, internal_id, username, name, location, source_type,
                       require_location, enabled, priority, added_via,quality_since)
                   VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(chat_id) DO UPDATE SET
                       internal_id=excluded.internal_id,
                       username=excluded.username,
                       name=excluded.name,
                       location=COALESCE(excluded.location, sources.location),
                       source_type=excluded.source_type,
                       require_location=excluded.require_location,
                       priority=excluded.priority""",
                (c.chat_id, c.id, c.username, c.name, c.location, c.source_type,
                 int(c.require_location), int(c.enabled), c.priority, added_via),
            )
            self.conn.execute("""UPDATE source_seeds SET resolved_chat_id=?
                WHERE internal_id=? OR lower(json_extract(chat_json,'$.username'))=lower(?)""",
                (c.chat_id,c.id,c.username))

    def upsert_sources(self, chats: Iterable[Chat], added_via: str = "config") -> None:
        for c in chats:
            self.upsert_source(c, added_via)

    def runtime_sources(self) -> list[Chat]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE enabled=1 ORDER BY priority DESC, name"
        ).fetchall()
        chats = [
            Chat(
                id=r["internal_id"], name=r["name"], chat_id=int(r["chat_id"]),
                username=r["username"], location=r["location"],
                source_type=r["source_type"] or "unclassified",
                require_location=bool(r["require_location"]), enabled=True,
                priority=int(r["priority"]), comment="runtime source",
            )
            for r in rows
        ]
        chats.extend(Chat(**json.loads(r["chat_json"])) for r in self.conn.execute(
            "SELECT chat_json FROM source_seeds WHERE resolved_chat_id IS NULL"
        ) if json.loads(r["chat_json"]).get("enabled",True))
        return chats

    def seed_sources_once(self) -> None:
        if self.runtime_value("sources_seeded_v1"):
            return
        # Only a new/migrating DB reads the historical bootstrap manifest.
        chats=[Chat(**r) for r in json.loads(Path(__file__).with_name("source_seed.json").read_text(encoding="utf-8"))]
        for chat in chats:
            found=self.conn.execute("SELECT * FROM sources WHERE chat_id=? OR (username IS NOT NULL AND lower(username)=lower(?))",
                                    (chat.chat_id,chat.username)).fetchone()
            if found:
                # Existing rows are authoritative, including their enabled state.
                if not chat.enabled and found["added_via"]=="config":
                    with self.conn:
                        self.conn.execute("UPDATE sources SET enabled=0 WHERE chat_id=?", (found["chat_id"],))
                continue
            if chat.chat_id is not None:
                self.upsert_source(chat)
            else:
                with self.conn:
                    self.conn.execute("INSERT OR IGNORE INTO source_seeds(internal_id,chat_json) VALUES(?,?)",
                                      (chat.id,json.dumps(asdict(chat),ensure_ascii=False)))
        self.set_runtime_value("sources_seeded_v1",_utc())

    def source(self, chat_id: int):
        return self.conn.execute("SELECT * FROM sources WHERE chat_id=?", (chat_id,)).fetchone()

    def source_chat(self, chat_id: int) -> Chat | None:
        r=self.source(chat_id)
        return Chat(id=r["internal_id"] or str(chat_id),name=r["name"] or str(chat_id),
                    chat_id=chat_id,username=r["username"],location=r["location"],
                    source_type=r["source_type"],priority=r["priority"],enabled=bool(r["enabled"])) if r else None

    def set_source_analysis(self, chat_id: int, enabled: bool, reason: str = "manual") -> bool:
        with self.conn:
            if enabled:
                cur=self.conn.execute("""UPDATE sources SET enabled=1,analysis_enabled=1,pause_reason=NULL,
                    paused_at=NULL,observed_seconds=0,quality_since=datetime('now') WHERE chat_id=?""", (chat_id,))
            else:
                cur=self.conn.execute("""UPDATE sources SET analysis_enabled=0,pause_reason=?,
                    paused_at=datetime('now') WHERE chat_id=?""", (reason,chat_id))
            return cur.rowcount>0

    def set_source_pinned(self, chat_id: int, pinned: bool) -> bool:
        with self.conn:
            return self.conn.execute("UPDATE sources SET pinned=? WHERE chat_id=?", (int(pinned),chat_id)).rowcount>0

    def source_health_tick(self) -> None:
        now=time.time()
        previous=float(self.runtime_value("source_health_tick") or now)
        # Do not count container downtime as days of successful observation.
        elapsed=max(0,min(90,int(now-previous)))
        with self.conn:
            self.conn.execute("UPDATE sources SET observed_seconds=observed_seconds+? WHERE enabled=1 AND analysis_enabled=1", (elapsed,))
            self.conn.execute("INSERT OR REPLACE INTO runtime_state VALUES('source_health_tick',?)", (str(now),))

    def source_health(self, days: int = 30) -> list[dict]:
        rows=self.conn.execute("""SELECT s.*,
            COUNT(DISTINCT m.text_hash) AS unique_messages,
            COUNT(DISTINCT CASE WHEN v.label IN ('direct','indirect')
                AND COALESCE(l.status,parent.status,'new')<>'rejected' THEN m.text_hash END) AS useful,
            COUNT(DISTINCT CASE WHEN (j.state IS NOT NULL AND j.state<>'done') OR v.label='review'
                THEN m.id END) AS unresolved
            FROM sources s LEFT JOIN messages m ON m.chat_id=s.chat_id
                AND m.created_at>=s.quality_since AND m.created_at>=datetime('now',?)
            LEFT JOIN message_semantics v ON v.message_id=m.id
            LEFT JOIN semantic_jobs j ON j.message_id=m.id
            LEFT JOIN leads l ON l.message_id=m.id
            LEFT JOIN leads parent ON parent.id=v.duplicate_of
            GROUP BY s.chat_id ORDER BY useful DESC,unique_messages DESC""", (f"-{int(days)} days",)).fetchall()
        return [dict(r) for r in rows]

    # Requests survive container rebuilds; invite links stay in the data volume.
    def source_request(self, key: str):
        return self.conn.execute("SELECT * FROM source_requests WHERE link_key=?", (key,)).fetchone()

    def enqueue_source(self, key: str, url: str, notify: bool = True) -> bool:
        with self.conn:
            old = self.source_request(key)
            if old and old["status"] not in {"failed", "active"}:
                if notify:
                    self.conn.execute("UPDATE source_requests SET notify=1 WHERE link_key=?", (key,))
                return False
            self.conn.execute(
                """INSERT INTO source_requests(link_key,url,notify) VALUES(?,?,?)
                   ON CONFLICT(link_key) DO UPDATE SET status='queued', next_attempt_at=0,
                       last_error=NULL, approval_requested=0, notify=excluded.notify""", (key, url, int(notify)),
            )
        return True

    def next_source_request(self):
        return self.conn.execute(
            """SELECT * FROM source_requests WHERE status IN ('queued','retry','pending','flood_wait')
               AND next_attempt_at<=? ORDER BY next_attempt_at,created_at,link_key LIMIT 1""",
            (time.time(),),
        ).fetchone()

    def source_cooldown_until(self) -> float:
        return self.conn.execute(
            "SELECT COALESCE(MAX(next_attempt_at),0) FROM source_requests WHERE status='flood_wait'"
        ).fetchone()[0]

    def finish_source_request(self, key: str, status: str, *, chat_id: int | None = None,
                              error: str | None = None, delay: float = 0) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE source_requests SET status=?,chat_id=COALESCE(?,chat_id),
                   last_error=?,next_attempt_at=?,attempts=attempts+1,
                   approval_requested=CASE WHEN ?='pending' THEN 1 ELSE approval_requested END
                   WHERE link_key=?""",
                (status, chat_id, error, time.time() + delay, status, key),
            )

    def source_request_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM source_requests GROUP BY status"
        )}

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

    # ---------- opportunities ----------
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
