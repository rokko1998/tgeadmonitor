import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from src.config.loader import Chat
from src.maintenance import Maintenance
from src.processing.pipeline import Pipeline
from src.processing.prefilter import opportunity_key, prefilter
from src.processing.semantic_policy import VERSION, SemanticDecision, decide, load_questions, validate_response
from src.processing.semantic_worker import SemanticWorker
from src.storage.database import Database, _utc
from src.storage.models import IncomingMessage
from src.storage.semantic import MSK
from src.telegram.notifier import HELP, Notifier

ROOT = Path(__file__).resolve().parents[1]
REVIEWED = [json.loads(s) for s in (ROOT / 'data/semantic_reviewed.jsonl').read_text().splitlines()]
ANSWERS = [json.loads(s) for s in (ROOT / 'data/semantic_answers_reviewed.jsonl').read_text().splitlines()]
ORIGINAL = [json.loads(s) for s in (ROOT / 'data/dataset.jsonl').read_text().splitlines()]


def incoming(n=1, text='Куплю арматуру А500С 12 мм, 3 тонны для фундамента'):
    return IncomingMessage(chat_id=-100123, chat_title='Test chat', telegram_message_id=n,
                           user_id=10, username='buyer_test', first_name='Buyer', text=text,
                           date=datetime.now(timezone.utc), message_url=f'https://t.me/test_chat/{n}')


def queue(db, cfg, metrics, n=1, text=None):
    Pipeline(cfg, db, None, metrics).handle(incoming(n, text) if text else incoming(n))
    return db.claim_semantic()


def response_for(questions, **selected):
    choices = dict(intent='demand', scope='metal_material', detail='specified',
                   operation='supply', business_fit='direct')
    choices.update(selected)
    return {'model': 'jev-1.14.0', 'usage': {'input_tokens': 3500, 'output_tokens': 80},
            'answers': {k: {'type': 'choice', 'choice': choices[k], 'confidence': 1,
                           'probabilities': {option: float(option == choices[k]) for option in q['criteria']}}
                        for k, q in questions.items()}}


class Response:
    def __init__(self, data=None, code=200):
        self.data, self.status, self.headers = data, code, {'retry-after': '5'}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return self.data


class Session:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize('case', [r for r in REVIEWED if r['expected'] == 'indirect'], ids=lambda r: str(r['reviewed_number']))
def test_prefilter_retains_every_owner_positive(case):
    assert prefilter(case['text'], datetime(2026, 9, 25, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize('case', [r for r in ORIGINAL if r['expected']])
def test_prefilter_retains_original_target_requests(case):
    assert prefilter(case['text']) is None


@pytest.mark.parametrize('case', ANSWERS, ids=lambda r: r['case_hash'][:8])
def test_recorded_jev_answers_compose_to_owner_labels(case):
    questions, prompt_hash = load_questions(ROOT / 'config')
    assert prompt_hash == case['prompt_hash']
    validate_response(case['response'], questions)
    assert decide(case['response']['answers']).label == case['expected']


@pytest.mark.parametrize('text,reason', [
    ('Предоставляем услуги разнорабочих и мастеров. Сантехники 6500, сварщики 7000', 'explicit_supplier_advertisement'),
    ('Наш спектр услуг: сантехники, сварщики, монолитчики', 'explicit_supplier_advertisement'),
    ('Нужно разгрузить машину с досками 3 куба', 'handling_existing_goods'),
    ('Куплю новый телефон, какой лучше?', 'no_relevant_subject'),
    ('Нужен подрядчик на капремонт. Окончание приема заявок 20.10.2026', 'expired_submission_deadline'),
])
def test_cheap_noise_rejections(text, reason):
    assert prefilter(text, datetime(2026, 10, 21, tzinfo=timezone.utc)) == reason


@pytest.mark.parametrize('text', [
    'Продам остатки, а также куплю арматуру 12мм 3 тонны',
    'Кто может привезти арматуру 3 тонны?', 'Нужен евроштакетник 100 метров',
    'Нужна труба ВГП 25, 120 метров', 'Ищу сварщика сделать ворота и калитку',
])
def test_mixed_demand_and_technical_names_are_preserved(text):
    assert prefilter(text) is None


def test_known_project_reposts_share_key():
    cases = {r['reviewed_number']: r['text'] for r in REVIEWED}
    assert opportunity_key(cases[5]) is not None
    assert opportunity_key(cases[5]) == opportunity_key(cases[7])
    assert opportunity_key('Нужен сантехник') is None
    assert opportunity_key(cases[5]) != opportunity_key(cases[15])


def test_direct_requires_independent_evidence(cfg):
    questions, _ = load_questions(ROOT / 'config')
    data = response_for(questions, operation='staffing', intent='employment')
    assert decide(data['answers']).label == 'review'
    data = response_for(questions, business_fit='indirect', intent='employment', operation='staffing', scope='adjacent_service')
    assert decide(data['answers']).label == 'indirect'
    data['answers']['intent']['probabilities']['offer'] = .6
    assert decide(data['answers']).label == 'review'
    data = response_for(questions, business_fit='indirect', intent='discussion', operation='none')
    assert decide(data['answers']).label == 'review'


def test_bad_probabilities_are_rejected():
    questions, _ = load_questions(ROOT / 'config')
    data = response_for(questions)
    data['answers']['scope']['probabilities']['outside'] = float('nan')
    with pytest.raises(ValueError):
        validate_response(data, questions)


def test_pipeline_only_queues_no_score_bypass(db, cfg, metrics):
    row = queue(db, cfg, metrics)
    assert row and row['original_text'] == incoming().text
    assert not db.pending_notifications()
    assert not db.conn.execute('SELECT * FROM leads').fetchall()
    assert db.conn.execute('SELECT score FROM messages').fetchone()[0] == 0


def test_paused_and_history_messages_are_archived_without_api(db, cfg, metrics):
    db.set_source_analysis(-100123, False)
    Pipeline(cfg, db, None, metrics, store_all=False).handle(incoming())
    assert db.conn.execute('SELECT status FROM messages').fetchone()[0] == 'archived'
    assert db.claim_semantic() is None
    db.set_source_analysis(-100123, True)
    Pipeline(cfg, db, None, metrics, archive_only=True).handle(incoming(2))
    assert db.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
    assert db.claim_semantic() is None


def test_budget_reservations_survive_crash_and_enforce_cap(tmp_path):
    path = tmp_path / 'ledger.db'
    db = Database(path)
    attempt = db.reserve_semantic('first', 100, 4200)
    assert attempt
    db.close()
    db = Database(path)
    try:
        assert db.reserve_semantic('second', 1, 4200) is None
        db.finish_semantic_attempt(attempt, input_tokens=20, state='response')
        assert db.reserve_semantic('second', 80, 4200)
        assert db.semantic_stats()['spent_or_reserved_usd'] == 4200 / 1e9
    finally:
        db.close()


def test_atomic_outbox_rolls_back_when_formatting_fails(db, cfg, metrics):
    row = queue(db, cfg, metrics)
    def fail(_):
        raise ValueError('formatting failure')
    with pytest.raises(ValueError):
        db.complete_semantic(row['message_id'], None, SemanticDecision('direct', 'test', 1), VERSION,
                             lead_fields={'source_type': 'legacy'}, payload_builder=fail)
    assert not db.conn.execute('SELECT * FROM message_semantics').fetchall()
    assert not db.conn.execute('SELECT * FROM leads').fetchall()
    assert not db.pending_notifications()


def test_recovery_requeues_interrupted_and_orphaned_jobs(tmp_path, cfg, metrics):
    path = tmp_path / 'recovery.db'
    db = Database(path)
    db.upsert_source(cfg.chats[0])
    row = queue(db, cfg, metrics)
    assert row
    db.close()
    db = Database(path)
    try:
        assert db.claim_semantic()['message_id'] == row['message_id']
    finally:
        db.close()


def test_worker_persists_evidence_and_reuses_cache(db, cfg, metrics, settings, notifier):
    worker = SemanticWorker(db, settings, notifier, metrics)
    session = Session(Response(response_for(worker.questions)))
    asyncio.run(worker.process(session, queue(db, cfg, metrics)))
    asyncio.run(worker.process(session, queue(db, cfg, metrics, n=2)))
    assert session.calls == 1
    assert len(db.pending_notifications()) == 2  # distinct short requests are not project-deduplicated
    assert db.conn.execute('SELECT COUNT(*) FROM semantic_evaluations').fetchone()[0] == 1
    assert db.conn.execute('SELECT COUNT(*) FROM message_semantics').fetchone()[0] == 2
    assert db.semantic_stats()['spent_or_reserved_usd'] == 3500 * 42 / 1e9


def test_project_duplicate_never_calls_api_or_sends_second_card(db, cfg, metrics, settings, notifier):
    worker = SemanticWorker(db, settings, notifier, metrics)
    cases = {r['reviewed_number']: r['text'] for r in REVIEWED}
    session = Session(Response(response_for(worker.questions, business_fit='indirect', scope='adjacent_service', operation='contractor')))
    asyncio.run(worker.process(session, queue(db, cfg, metrics, text=cases[5].replace('09.10.2026', '09.10.2099'))))
    asyncio.run(worker.process(session, queue(db, cfg, metrics, n=2, text=cases[7].replace('09.10.2026', '09.10.2099'))))
    assert session.calls == 1
    assert len(db.pending_notifications()) == 1
    assert db.conn.execute('SELECT duplicate_of FROM message_semantics WHERE message_id=2').fetchone()[0]
    assert len(db.indirect_since('2000', '2100')) == 1


@pytest.mark.parametrize('code,state,charged_zero', [(429, 'queued', True), (401, 'failed', False), (500, 'queued', False)])
def test_http_failures_keep_messages_and_never_fallback(db, cfg, metrics, settings, notifier, code, state, charged_zero):
    worker = SemanticWorker(db, settings, notifier, metrics)
    asyncio.run(worker.process(Session(Response(code=code)), queue(db, cfg, metrics)))
    assert not db.pending_notifications()
    assert db.conn.execute('SELECT state FROM semantic_jobs').fetchone()[0] == state
    assert (db.semantic_stats()['spent_or_reserved_usd'] == 0) == charged_zero


def test_transport_uncertainty_keeps_reservation(db, cfg, metrics, settings, notifier):
    worker = SemanticWorker(db, settings, notifier, metrics)
    asyncio.run(worker.process(Session(error=aiohttp.ClientError()), queue(db, cfg, metrics)))
    assert db.semantic_stats()['spent_or_reserved_usd'] > 0
    assert not db.pending_notifications()


def test_invalid_response_keeps_usage_no_notification(db, cfg, metrics, settings, notifier):
    worker = SemanticWorker(db, settings, notifier, metrics)
    data = response_for(worker.questions)
    del data['answers']['intent']
    asyncio.run(worker.process(Session(Response(data)), queue(db, cfg, metrics)))
    assert not db.pending_notifications()
    assert db.semantic_stats()['jobs']['failed'] == 1
    assert db.semantic_stats()['spent_or_reserved_usd'] == 3500 * 42 / 1e9


def test_budget_zero_prevents_http(db, cfg, metrics, settings, notifier):
    worker = SemanticWorker(db, replace(settings, jev_monthly_budget_usd=0), notifier, metrics)
    session = Session()
    asyncio.run(worker.process(session, queue(db, cfg, metrics)))
    assert session.calls == 0
    assert not db.pending_notifications()
    assert db.semantic_stats()['jobs']['queued'] == 1


def test_seed_migration_never_undoes_database_pause(db, tmp_path):
    db.seed_sources_once()
    db.set_source_analysis(-100123, False)
    db.set_source_pinned(-100123, True)
    db.upsert_source(Chat(id='test', name='Updated name', chat_id=-100123))
    db.seed_sources_once()
    assert not db.source(-100123)['analysis_enabled']
    assert db.source(-100123)['pinned']
    assert -100123 in {c.chat_id for c in db.runtime_sources()}


def test_autojoin_survives_database_roundtrip_but_never_resumes_pause(db):
    chat=Chat(id='test',name='Test',chat_id=-100123,username='test_chat',auto_join=True)
    db.upsert_source(chat)
    restored=db.source_chat(-100123)
    assert restored.auto_join and db.should_auto_join(restored)
    db.set_source_analysis(-100123,False)
    db.upsert_source(Chat(id='test',name='Updated metadata',chat_id=-100123))
    assert db.source_chat(-100123).auto_join
    assert not db.should_auto_join(db.source_chat(-100123))


def test_autojoin_one_time_migration_preserves_pause_and_does_not_repeat(db):
    chat=Chat(id='Avito_Remont',name='Known configured',chat_id=-100456,username='Avito_Remont')
    db.upsert_source(chat)
    db.set_source_analysis(chat.chat_id,False)
    db.seed_sources_once()
    assert db.source(chat.chat_id)['auto_join']
    assert not db.source(chat.chat_id)['analysis_enabled']
    with db.conn:
        db.conn.execute('UPDATE sources SET auto_join=0 WHERE chat_id=?',(chat.chat_id,))
    db.seed_sources_once()
    assert not db.source(chat.chat_id)['auto_join']


def test_digest_cutoff_restart_and_after_21_are_lossless(db, cfg, metrics, settings, notifier):
    worker = SemanticWorker(db, settings, notifier, metrics)
    row = queue(db, cfg, metrics)
    worker.complete(row, None, SemanticDecision('indirect', 'test', 1))
    with db.conn:
        db.conn.execute("UPDATE leads SET created_at='2026-10-21 18:30:00'")  # 21:30 MSK
    db.set_runtime_value('semantic_started_at_v1', '2026-10-21 17:00:00')
    maintenance = Maintenance(db, settings, notifier)
    maintenance.digest(datetime(2026, 10, 21, 22, tzinfo=MSK))
    assert len(db.pending_system_notifications()) == 1
    assert 'нет.' in json.loads(db.pending_system_notifications()[0]['payload'])['text']
    maintenance.digest(datetime(2026, 10, 21, 23, tzinfo=MSK))
    assert len(db.pending_system_notifications()) == 1
    maintenance.digest(datetime(2026, 10, 22, 21, tzinfo=MSK))
    assert len(db.pending_system_notifications()) == 2
    assert 'Всего без повторов: 1' in json.loads(db.pending_system_notifications()[1]['payload'])['text']


def test_quality_requires_observed_time_unique_volume_and_no_unresolved(db, cfg, metrics, settings, notifier):
    maintenance = Maintenance(db, replace(settings, source_quality_min_messages=2), notifier)
    queue(db, cfg, metrics)
    Pipeline(cfg, db, None, metrics).handle(incoming(2, 'Поговорим о погоде'))
    with db.conn:
        db.conn.execute("UPDATE sources SET quality_since=datetime('now','-31 days'), observed_seconds=?", (30*86400,))
    maintenance.assess_sources()
    assert db.source(-100123)['analysis_enabled']  # queued API failure must not trigger auto-pause
    db.complete_semantic(1, None, SemanticDecision('ignore', 'test', 1), VERSION)
    db.set_source_pinned(-100123, True)
    maintenance.assess_sources()
    assert db.source(-100123)['analysis_enabled']
    db.set_source_pinned(-100123, False)
    maintenance.assess_sources()
    assert not db.source(-100123)['analysis_enabled']
    assert db.source(-100123)['enabled']
    assert db.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2


def test_downtime_does_not_count_as_observation(db):
    db.set_runtime_value('source_health_tick', str(time.time() - 100000))
    db.source_health_tick()
    assert db.source(-100123)['observed_seconds'] <= 90


def test_configured_group_requires_admin_and_help_explains_workflow(db, metrics):
    bot = Notifier('unused', -12345, db, metrics)
    bot.api = AsyncMock(return_value={'status': 'member'})
    assert not asyncio.run(bot._authorized({'id': 42}, -12345))
    bot.api.return_value = {'status': 'administrator'}
    assert asyncio.run(bot._authorized({'id': 42}, -12345))
    assert not asyncio.run(bot._authorized({'id': 42}, -99999))
    for token in ('Непрямой лид', '21:00', '/resume', '/keep', '/jev', '@example_chat', '/jevretry'):
        assert token in HELP


def test_chat_list_distinguishes_disconnected_sources(db, metrics):
    bot=Notifier('unused',42,db,metrics)
    bot.api=AsyncMock()
    bot.set_chat_manager(SimpleNamespace(listener=SimpleNamespace(resolved={})))
    update={'message':{'chat':{'id':42},'from':{'id':42},'text':'/chats'}}
    asyncio.run(bot._handle_update(update))
    assert 'Нет подключения' in bot.api.await_args.kwargs['text']
    bot.chat_manager.listener.resolved[-100123]=object()
    asyncio.run(bot._handle_update(update))
    assert '✅ Jev' in bot.api.await_args.kwargs['text']
