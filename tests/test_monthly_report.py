import asyncio
import json
import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from src.maintenance import Maintenance
from src.reporting import format_monthly_report
from src.storage.database import Database
from src.time_utils import MSK, month_bounds
from src.telegram.notifier import Notifier


def insert_message(db, n, created_at, status="prefiltered", chat_id=-100123):
    with db.conn:
        return db.conn.execute("""INSERT INTO messages(
            telegram_message_id,chat_id,original_text,normalized_text,text_hash,status,created_at)
            VALUES(?,?,?,?,?,?,?)""", (n,chat_id,"test","test",str(n),status,created_at)).lastrowid


@pytest.fixture
def october_data(db):
    a = insert_message(db, 1, "2026-09-30 21:00:00", "lead")
    b = insert_message(db, 2, "2026-10-31 20:59:59", "indirect")
    insert_message(db, 3, "2026-10-05 12:00:00", "archived", chat_id=-100456)
    insert_message(db, 4, "2026-10-05 12:01:00", "duplicate")
    previous = insert_message(db, 5, "2026-09-30 20:59:59", "lead")
    future = insert_message(db, 6, "2026-10-31 21:00:00", "lead")
    with db.conn:
        db.conn.executemany("INSERT INTO leads(message_id,lead_type,status,created_at) VALUES(?,?,?,?)", [
            (a,"lead","rejected","2026-10-01 00:00:00"),
            (b,"indirect","new","2026-10-31 20:59:59"),
            (previous,"lead","new","2026-10-02 00:00:00"),
            (future,"lead","new","2026-10-31 21:00:00"),
        ])
        db.conn.executemany("""INSERT INTO semantic_attempts(
            month,cache_key,reserved_nano,charged_nano,state) VALUES(?,?,?,?,?)""", [
            ("2026-10","paid",400_000_000,250_000_000,"response"),
            ("2026-10","uncertain",100_000_000,None,"uncertain_transport"),
            ("2026-10","rate_limit",50_000_000,0,"http_error"),
            ("2026-11","next_month",900_000_000,750_000_000,"response"),
        ])
    return db


def test_monthly_totals_use_moscow_boundaries_and_keep_archive_separate(october_data):
    stats = october_data.monthly_report_stats("2026-10")
    assert stats["since"] == "2026-09-30 21:00:00"
    assert stats["until"] == "2026-10-31 21:00:00"
    assert (stats["messages"],stats["chats"]) == (4,2)
    assert (stats["processed_messages"],stats["processed_chats"],stats["archived_messages"]) == (3,1,1)
    assert (stats["direct_leads"],stats["indirect_leads"]) == (2,1)
    assert (stats["rejected_direct"],stats["rejected_indirect"]) == (1,0)
    assert stats["spent_nano"] == 250_000_000
    assert stats["reserved_nano"] == 100_000_000
    assert stats["api_attempts"] == 3
    body = format_monthly_report(stats)["text"]
    assert "октябрь 2026" in body and "Обработано сообщений: 3" in body
    assert "$0.2500" in body and "$0.1000" in body


def test_empty_month_has_zero_counts_and_no_unknown_cost(db):
    stats = db.monthly_report_stats("2026-09")
    assert stats["messages"] == stats["chats"] == stats["direct_leads"] == stats["indirect_leads"] == 0
    body = format_monthly_report(stats)["text"]
    assert "$0.0000" in body and "зарезервировано" not in body


@pytest.mark.parametrize("period",["2026-1","2026-00","2026-13","hello",""])
def test_invalid_calendar_month_is_rejected(period):
    with pytest.raises(ValueError):
        month_bounds(period)


def test_leap_february_and_december_rollover():
    start,end = month_bounds("2028-02")
    assert (end-start).days == 29
    start,end = month_bounds("2026-12")
    assert end == datetime(2027,1,1,tzinfo=MSK)


def test_current_month_cutoff_excludes_later_messages(october_data):
    stats = october_data.monthly_report_stats("2026-10",through=datetime(2026,10,4,9,tzinfo=MSK))
    assert stats["messages"] == 1
    assert stats["indirect_leads"] == 0


def test_first_report_at_nine_and_restart_does_not_form_duplicate(tmp_path, settings):
    path = tmp_path / "monthly.sqlite"
    db = Database(path)
    worker = Maintenance(db,settings,Mock())
    worker.monthly_report(datetime(2026,10,21,15,tzinfo=MSK))
    assert not db.pending_system_notifications()
    worker.monthly_report(datetime(2026,11,1,8,59,59,tzinfo=MSK))
    assert not db.pending_system_notifications()
    worker.monthly_report(datetime(2026,11,1,9,tzinfo=MSK))
    assert len(db.pending_system_notifications()) == 1
    assert db.runtime_value("monthly_report_next_period") == "2026-11"
    db.close()
    db = Database(path)
    try:
        Maintenance(db,settings,Mock()).monthly_report(datetime(2026,11,1,10,tzinfo=MSK))
        assert len(db.pending_system_notifications()) == 1
        row = db.pending_system_notifications()[0]
        assert row["key"] == "monthly-report:2026-10"
        assert json.loads(row["payload"])["monthly_stats"]["period"] == "2026-10"
    finally:
        db.close()


def test_catchup_generates_each_missed_month_once(db,settings,notifier):
    worker = Maintenance(db,settings,notifier)
    worker.monthly_report(datetime(2026,10,21,15,tzinfo=MSK))
    worker.monthly_report(datetime(2027,1,1,9,tzinfo=MSK))
    assert [r["key"] for r in db.pending_system_notifications()] == [
        "monthly-report:2026-10","monthly-report:2026-11","monthly-report:2026-12"]
    assert db.runtime_value("monthly_report_next_period") == "2027-01"
    worker.monthly_report(datetime(2027,1,2,9,tzinfo=MSK))
    assert len(db.pending_system_notifications()) == 3


def test_snapshot_and_checkpoint_rollback_together(db,settings,notifier):
    worker = Maintenance(db,settings,notifier)
    worker.monthly_report(datetime(2026,10,21,15,tzinfo=MSK))
    db.conn.execute("""CREATE TRIGGER reject_monthly_checkpoint BEFORE INSERT ON runtime_state
        WHEN NEW.key='monthly_report_next_period' AND NEW.value='2026-11'
        BEGIN SELECT RAISE(ABORT,'checkpoint write failed'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        worker.monthly_report(datetime(2026,11,1,9,tzinfo=MSK))
    assert not db.pending_system_notifications()
    assert db.runtime_value("monthly_report_next_period") == "2026-10"
    db.conn.execute("DROP TRIGGER reject_monthly_checkpoint")
    worker.monthly_report(datetime(2026,11,1,9,tzinfo=MSK))
    assert len(db.pending_system_notifications()) == 1


def test_monthly_delivery_backoff_survives_restart_and_exceeds_fifty_retries(tmp_path):
    path = tmp_path / "delivery.sqlite"
    db = Database(path)
    db.enqueue_system("monthly-report:2026-10",{"text":"monthly report"})
    row = db.pending_system_notifications()[0]
    db.finish_system_notification(row["id"],"network_error")
    assert not db.pending_system_notifications()
    db.close()
    db = Database(path)
    try:
        assert not db.pending_system_notifications()
        with db.conn:
            db.conn.execute("UPDATE system_notifications SET attempts=60,next_attempt=0")
        row = db.pending_system_notifications()[0]
        db.finish_system_notification(row["id"])
        assert not db.pending_system_notifications()
        assert db.conn.execute("SELECT status FROM system_notifications").fetchone()[0] == "sent"
    finally:
        db.close()


def test_existing_outbox_migration_preserves_pending_report(tmp_path):
    path=tmp_path / "old-schema.sqlite"
    connection=sqlite3.connect(path)
    connection.execute("""CREATE TABLE system_notifications(
        id INTEGER PRIMARY KEY,key TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,created_at TEXT NOT NULL DEFAULT (datetime('now')),sent_at TEXT)""")
    connection.execute("INSERT INTO system_notifications(key,payload) VALUES(?,?)",
                       ("monthly-report:2026-10",'{"text":"saved report"}'))
    connection.commit()
    connection.close()
    db=Database(path)
    try:
        row=db.pending_system_notifications()[0]
        assert row["next_attempt"]==0
        assert json.loads(row["payload"])["text"]=="saved report"
    finally:
        db.close()


def test_manual_command_reuses_frozen_report(db,settings,metrics,notifier):
    worker = Maintenance(db,settings,notifier)
    worker.monthly_report(datetime(2000,1,15,9,tzinfo=MSK))
    worker.monthly_report(datetime(2000,2,1,9,tzinfo=MSK))
    snapshot = db.pending_system_notifications()[0]["payload"]
    insert_message(db,1,"2000-01-15 00:00:00")
    bot = Notifier("unused",42,db,metrics)
    bot.api = AsyncMock()
    asyncio.run(bot._handle_update({"message":{"chat":{"id":42},"from":{"id":42},"text":"/month 2000-01"}}))
    assert bot.api.await_args.kwargs["text"] == json.loads(snapshot)["text"]
    assert len(db.pending_system_notifications()) == 1


@pytest.mark.parametrize("argument",["2026-13","2026-1","2100-01","2026-10 extra"])
def test_manual_command_explains_invalid_period(db,metrics,argument):
    bot = Notifier("unused",42,db,metrics)
    bot.api = AsyncMock()
    asyncio.run(bot._handle_update({"message":{"chat":{"id":42},"from":{"id":42},"text":"/month "+argument}}))
    assert "Формат: /month" in bot.api.await_args.kwargs["text"]
