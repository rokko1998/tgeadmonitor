"""Daily and monthly reporting and conservative source analysis pauses."""
from __future__ import annotations

import asyncio
import logging
import json
from datetime import datetime, timedelta, timezone

from src.settings import Settings
from src.reporting import format_monthly_report
from src.storage.database import Database, _utc
from src.time_utils import MSK, month_bounds
from src.telegram.formatter import build_indirect_digest

log=logging.getLogger("maintenance")


class Maintenance:
    def __init__(self, db: Database, settings: Settings, notifier):
        self.db,self.settings,self.notifier=db,settings,notifier

    def digest(self, now: datetime | None = None) -> None:
        now=(now or datetime.now(MSK)).astimezone(MSK)
        cutoff=now.replace(hour=self.settings.digest_hour_msk,minute=0,second=0,microsecond=0)
        if now<cutoff:cutoff-=timedelta(days=1)
        until=_utc(cutoff.astimezone(timezone.utc))
        since=self.db.runtime_value("indirect_digest_cutoff") or self.db.runtime_value("semantic_started_at_v1") or until
        if since>=until:return
        rows=self.db.indirect_since(since,until)
        start=datetime.fromisoformat(since).replace(tzinfo=timezone.utc).astimezone(MSK)
        period=f"{start:%d.%m %H:%M} — {cutoff:%d.%m %H:%M} МСК"
        payloads=build_indirect_digest(rows,period)
        # Enqueue the complete recap and checkpoint together; restarts cannot
        # duplicate part of it or omit messages received after the cutoff.
        with self.db.conn:
            for i,payload in enumerate(payloads):
                self.db.conn.execute("INSERT OR IGNORE INTO system_notifications(key,payload) VALUES(?,?)",
                    (f"indirect-digest:{until}:{i}",json.dumps(payload,ensure_ascii=False)))
            self.db.conn.execute("INSERT OR REPLACE INTO runtime_state VALUES('indirect_digest_cutoff',?)", (until,))
        self.notifier.wakeup()

    def monthly_report(self, now: datetime | None = None) -> None:
        now = (now or datetime.now(MSK)).astimezone(MSK)
        cursor = self.db.runtime_value("monthly_report_next_period")
        if cursor is None:
            # Start with the current month; installing this feature must not
            # unexpectedly send reports for every historic month in the DB.
            cursor = now.strftime("%Y-%m")
            self.db.set_runtime_value("monthly_report_next_period", cursor)
        while True:
            _, end = month_bounds(cursor)
            due = end.replace(hour=self.settings.monthly_report_hour_msk)
            if now < due:
                return
            # Persist the report snapshot, outbox entry and next period together.
            # A restart catches up each missed month without forming it twice.
            with self.db.conn:
                self.db.conn.execute("BEGIN IMMEDIATE")
                stats = self.db.monthly_report_stats(cursor)
                payload = {**format_monthly_report(stats), "monthly_stats": stats}
                self.db.conn.execute("INSERT OR IGNORE INTO system_notifications(key,payload) VALUES(?,?)",
                                     (f"monthly-report:{cursor}",json.dumps(payload,ensure_ascii=False)))
                cursor = end.strftime("%Y-%m")
                self.db.conn.execute("INSERT OR REPLACE INTO runtime_state VALUES('monthly_report_next_period',?)", (cursor,))
            self.notifier.wakeup()

    def assess_sources(self) -> None:
        days=self.settings.source_quality_days
        for source in self.db.source_health(days):
            if (not source["enabled"] or not source["analysis_enabled"] or source["pinned"]
                    or source["observed_seconds"]<days*86400
                    or source["unique_messages"]<self.settings.source_quality_min_messages
                    or source["useful"] or source["unresolved"]):
                continue
            reason=f"За {days} дней и {source['unique_messages']} уникальных сообщений нет прямых/непрямых возможностей."
            self.db.set_source_analysis(source["chat_id"],False,reason)
            self.db.enqueue_system(f"source-pause:{source['chat_id']}:{source['quality_since']}",{
                "text":f"⏸ Пауза Jev: {source['name']} ({source['chat_id']})\n{reason}\n"
                       f"Сообщения продолжают сохраняться в архив. Вернуть оценку: /resume {source['chat_id']}\n"
                       f"Защитить от автопаузы: /keep {source['chat_id']}"})
            self.notifier.wakeup()

    async def run(self) -> None:
        while True:
            try:
                self.db.source_health_tick()
                self.digest()
                self.monthly_report()
                self.assess_sources()
            except Exception:
                log.exception("Maintenance error")
            await asyncio.sleep(60)
