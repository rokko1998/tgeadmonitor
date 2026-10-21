"""Durable semantic jobs, reusable evidence, budget ledger and system outbox."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from src.time_utils import MSK, month_bounds

NANO_PER_TOKEN = 42  # $0.042 / 1M input tokens; output free, 2026-10-21.


def month_key() -> str:
    return datetime.now(MSK).strftime("%Y-%m")


class SemanticStorage:
    def init_semantic_storage(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS semantic_prompts(
          hash TEXT PRIMARY KEY,policy TEXT NOT NULL,questions TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS semantic_jobs(
          message_id INTEGER PRIMARY KEY REFERENCES messages(id),
          meta TEXT NOT NULL DEFAULT '{}',state TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
          last_error TEXT);
        CREATE INDEX IF NOT EXISTS semantic_jobs_ready ON semantic_jobs(state,next_attempt);
        CREATE TABLE IF NOT EXISTS semantic_evaluations(
          cache_key TEXT PRIMARY KEY,provider TEXT NOT NULL,requested_model TEXT NOT NULL,
          resolved_model TEXT NOT NULL,prompt_hash TEXT NOT NULL,policy TEXT NOT NULL,
          input_hash TEXT NOT NULL,response TEXT NOT NULL,input_tokens INTEGER NOT NULL,
          output_tokens INTEGER NOT NULL,cost_nano INTEGER NOT NULL,latency REAL NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS message_semantics(
          message_id INTEGER PRIMARY KEY REFERENCES messages(id),cache_key TEXT,
          label TEXT NOT NULL,reason TEXT NOT NULL,confidence REAL NOT NULL DEFAULT 0,
          policy TEXT NOT NULL,duplicate_of INTEGER,
          created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS semantic_attempts(
          id INTEGER PRIMARY KEY,month TEXT NOT NULL,cache_key TEXT NOT NULL,
          reserved_nano INTEGER NOT NULL,charged_nano INTEGER,state TEXT NOT NULL,
          error_code TEXT,created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE INDEX IF NOT EXISTS semantic_attempt_month ON semantic_attempts(month);
        CREATE TABLE IF NOT EXISTS opportunity_keys(
          key TEXT PRIMARY KEY,lead_id INTEGER NOT NULL REFERENCES leads(id),
          last_seen TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS system_notifications(
          id INTEGER PRIMARY KEY,key TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,created_at TEXT NOT NULL DEFAULT (datetime('now')),sent_at TEXT,
          next_attempt REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS source_seeds(
          internal_id TEXT PRIMARY KEY,chat_json TEXT NOT NULL,resolved_chat_id INTEGER);
        """)
        with self.conn:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(system_notifications)")}
            if "next_attempt" not in columns:
                self.conn.execute("ALTER TABLE system_notifications ADD COLUMN next_attempt REAL NOT NULL DEFAULT 0")
            # A crash may leave an accepted request's reservation unresolved; retain
            # its full reservation in the ledger rather than silently refunding it.
            self.conn.execute("UPDATE semantic_jobs SET state='queued' WHERE state='processing'")
            self.conn.execute("""INSERT OR IGNORE INTO semantic_jobs(message_id)
                SELECT id FROM messages WHERE status='semantic_pending'""")

    def runtime_value(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_runtime_value(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?)", (key, value))

    def queue_semantic(self, message_id: int, meta: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO semantic_jobs(message_id,meta) VALUES(?,?)",
                              (message_id, json.dumps(meta, ensure_ascii=False)))

    def claim_semantic(self):
        with self.conn:
            r = self.conn.execute("""SELECT j.*,m.original_text,m.chat_id,m.date,m.user_id,
                m.username,m.first_name,m.message_url,m.telegram_message_id,m.score,
                s.name AS chat_name,s.source_type,s.analysis_enabled
                FROM semantic_jobs j JOIN messages m ON m.id=j.message_id
                JOIN sources s ON s.chat_id=m.chat_id
                WHERE j.state='queued' AND j.next_attempt<=?
                ORDER BY j.message_id LIMIT 1""", (time.time(),)).fetchone()
            if r:
                self.conn.execute("UPDATE semantic_jobs SET state='processing' WHERE message_id=?", (r["message_id"],))
            return r

    def retry_semantic(self, message_id: int, code: str, delay: float, *, terminal: bool = False) -> None:
        with self.conn:
            self.conn.execute("""UPDATE semantic_jobs SET state=?,attempts=attempts+1,
                next_attempt=?,last_error=? WHERE message_id=?""",
                ("failed" if terminal else "queued", time.time()+delay, code, message_id))

    def cached_semantic(self, cache_key: str):
        return self.conn.execute("SELECT * FROM semantic_evaluations WHERE cache_key=?", (cache_key,)).fetchone()

    def register_semantic_prompt(self, prompt_hash: str, policy: str, questions: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO semantic_prompts VALUES(?,?,?)",
                              (prompt_hash, policy, json.dumps(questions, ensure_ascii=False)))

    def reserve_semantic(self, cache_key: str, tokens: int, monthly_limit_nano: int) -> int | None:
        month = month_key()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            used = self.conn.execute("""SELECT COALESCE(SUM(COALESCE(charged_nano,reserved_nano)),0)
                FROM semantic_attempts WHERE month=?""", (month,)).fetchone()[0]
            reserve = int(tokens) * NANO_PER_TOKEN
            if used + reserve > monthly_limit_nano:
                return None
            return int(self.conn.execute("""INSERT INTO semantic_attempts(
                month,cache_key,reserved_nano,state) VALUES(?,?,?,'pending')""",
                (month, cache_key, reserve)).lastrowid)

    def finish_semantic_attempt(self, attempt: int, *, input_tokens: int | None,
                                state: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE semantic_attempts SET charged_nano=?,state=?,error_code=? WHERE id=?",
                              (input_tokens*NANO_PER_TOKEN if input_tokens is not None else None,
                               state, error, attempt))

    def save_semantic_evidence(self, cache_key: str, provider: str, model: str,
                               prompt_hash: str, policy: str, input_hash: str,
                               response: dict, latency: float) -> None:
        usage = response["usage"]
        with self.conn:
            self.conn.execute("""INSERT OR IGNORE INTO semantic_evaluations VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                (cache_key, provider, model, response["model"], prompt_hash, policy,
                 input_hash, json.dumps(response, ensure_ascii=False), usage["input_tokens"],
                 usage["output_tokens"], usage["input_tokens"]*NANO_PER_TOKEN, latency))

    def duplicate_opportunity(self, key: str | None) -> int | None:
        if not key:
            return None
        r = self.conn.execute("""SELECT k.lead_id FROM opportunity_keys k JOIN leads l ON l.id=k.lead_id
            WHERE k.key=? AND k.last_seen>=datetime('now','-7 days') AND l.status<>'rejected'""", (key,)).fetchone()
        return int(r["lead_id"]) if r else None

    def complete_semantic(self, message_id: int, cache_key: str | None, decision,
                          policy: str, *, key: str | None = None, duplicate: int | None = None,
                          lead_fields: dict | None = None, payload_builder=None) -> int | None:
        """Atomically persist decision, opportunity and its outbox entry."""
        with self.conn:
            if self.conn.execute("SELECT 1 FROM message_semantics WHERE message_id=?", (message_id,)).fetchone():
                self.conn.execute("UPDATE semantic_jobs SET state='done' WHERE message_id=?", (message_id,))
                return None
            if key and duplicate is None:
                duplicate = self.duplicate_opportunity(key)
            self.conn.execute("INSERT INTO message_semantics VALUES(?,?,?,?,?,?,?,datetime('now'))",
                              (message_id, cache_key, decision.label, decision.reason,
                               decision.confidence, policy, duplicate))
            self.conn.execute("UPDATE messages SET status=? WHERE id=?",
                              ("duplicate" if duplicate else {"direct":"lead","ignore":"ignored"}.get(decision.label,decision.label),message_id))
            self.conn.execute("UPDATE semantic_jobs SET state='done',last_error=NULL WHERE message_id=?", (message_id,))
            if duplicate:
                if key:
                    self.conn.execute("UPDATE opportunity_keys SET last_seen=datetime('now') WHERE key=?", (key,))
                return None
            if decision.label not in ("direct", "indirect") or lead_fields is None:
                return None
            f = lead_fields
            lead_id = int(self.conn.execute("""INSERT INTO leads(message_id,lead_type,source_type,
                product,location,amount,size) VALUES(?,?,?,?,?,?,?)""",
                (message_id,"lead" if decision.label=="direct" else "indirect",f["source_type"],
                 f.get("product"),f.get("location"),f.get("amount"),f.get("size"))).lastrowid)
            if key:
                self.conn.execute("INSERT OR REPLACE INTO opportunity_keys VALUES(?,?,datetime('now'))", (key,lead_id))
            payload = payload_builder(lead_id)
            self.conn.execute("INSERT INTO notifications(lead_id,payload) VALUES(?,?)",
                              (lead_id,json.dumps(payload,ensure_ascii=False)))
            return lead_id

    def enqueue_system(self, key: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO system_notifications(key,payload) VALUES(?,?)",
                              (key,json.dumps(payload,ensure_ascii=False)))

    def pending_system_notifications(self):
        return self.conn.execute("""SELECT * FROM system_notifications
            WHERE status='pending' AND next_attempt<=?
              AND (attempts<50 OR key LIKE 'monthly-report:%')
            ORDER BY id LIMIT 20""", (time.time(),)).fetchall()

    def finish_system_notification(self, notification_id: int, error: str | None = None) -> None:
        with self.conn:
            if error:
                row = self.conn.execute("SELECT key,attempts FROM system_notifications WHERE id=?", (notification_id,)).fetchone()
                if row is None:
                    return
                delay = min(3600,30 * 2**min(row["attempts"],7)) if row["key"].startswith("monthly-report:") else 0
                self.conn.execute("UPDATE system_notifications SET attempts=attempts+1,last_error=?,next_attempt=? WHERE id=?",
                                  (error[:300],time.time()+delay,notification_id))
            else:
                self.conn.execute("UPDATE system_notifications SET status='sent',sent_at=datetime('now') WHERE id=?", (notification_id,))

    def indirect_since(self, since: str, until: str):
        return self.conn.execute("""SELECT l.id,l.status,l.product,m.original_text,m.message_url,
            s.name AS chat_name FROM leads l JOIN messages m ON m.id=l.message_id
            LEFT JOIN sources s ON s.chat_id=m.chat_id
            WHERE l.lead_type='indirect' AND l.status<>'rejected' AND l.created_at>=? AND l.created_at<?
            ORDER BY l.created_at,l.id""", (since,until)).fetchall()

    def semantic_stats(self) -> dict:
        period=month_key()
        cost=self.conn.execute("SELECT COALESCE(SUM(COALESCE(charged_nano,reserved_nano)),0) FROM semantic_attempts WHERE month=?", (period,)).fetchone()[0]
        return {"month":period,"spent_or_reserved_usd":cost/1_000_000_000,
                "jobs":{r["state"]:r["n"] for r in self.conn.execute("SELECT state,COUNT(*) n FROM semantic_jobs GROUP BY state")},
                "decisions":{r["label"]:r["n"] for r in self.conn.execute("SELECT label,COUNT(*) n FROM message_semantics GROUP BY label")}}

    def monthly_report_stats(self, period: str, *, through: datetime | None = None) -> dict:
        start, end = month_bounds(period)
        if through is not None:
            end = min(end, through.astimezone(MSK))
        since = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        until = end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        messages = self.conn.execute("""SELECT COUNT(*) AS messages,COUNT(DISTINCT chat_id) AS chats,
            COALESCE(SUM(status='archived'),0) AS archived_messages,
            COALESCE(SUM(status<>'archived'),0) AS processed_messages,
            COUNT(DISTINCT CASE WHEN status<>'archived' THEN chat_id END) AS processed_chats
            FROM messages WHERE created_at>=? AND created_at<?""", (since,until)).fetchone()
        leads = self.conn.execute("""SELECT COALESCE(SUM(lead_type='lead'),0) AS direct_leads,
            COALESCE(SUM(lead_type IN ('indirect','project_signal')),0) AS indirect_leads,
            COALESCE(SUM(lead_type='lead' AND status='rejected'),0) AS rejected_direct,
            COALESCE(SUM(lead_type IN ('indirect','project_signal') AND status='rejected'),0) AS rejected_indirect
            FROM leads WHERE created_at>=? AND created_at<?""", (since,until)).fetchone()
        cost = self.conn.execute("""SELECT COUNT(*) AS api_attempts,
            COALESCE(SUM(charged_nano),0) AS spent_nano,
            COALESCE(SUM(CASE WHEN charged_nano IS NULL THEN reserved_nano ELSE 0 END),0) AS reserved_nano
            FROM semantic_attempts WHERE month=?""", (period,)).fetchone()
        return {"period":period,"since":since,"until":until,**dict(messages),**dict(leads),**dict(cost)}
