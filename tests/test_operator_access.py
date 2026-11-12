import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.metrics import Metrics
from src.storage.database import Database
from src.telegram.notifier import BotApiError, Notifier


def group_bot(db, status='member', **member_fields):
    bot = Notifier('unused', -12345, db, Metrics())
    async def api(method, **params):
        if method == 'getChatMember':
            assert params['chat_id'] == -12345
            return {'status':status, **member_fields}
        return {'message_id':1}
    bot.api = AsyncMock(side_effect=api)
    manager = Mock()
    manager.submit.return_value = 'в очереди на добавление'
    manager.listener = SimpleNamespace(resolved={})
    bot.set_chat_manager(manager)
    return bot


@pytest.mark.parametrize('text', ['https://t.me/example_chat', '/addchat @example_chat @another_chat'])
def test_allowed_member_can_add_chats_and_receives_reply(db,text):
    db.grant_operator(-12345,42,'Test operator')
    bot = group_bot(db)
    asyncio.run(bot._handle_update({'message':{'chat':{'id':-12345},'from':{'id':42},'text':text}}))
    assert bot.chat_manager.submit.call_count == (2 if 'another_chat' in text else 1)
    replies = [c.kwargs for c in bot.api.await_args_list if c.args[0] == 'sendMessage']
    assert len(replies) == 1 and replies[0]['chat_id'] == -12345
    assert 'Результат приёма ссылок' in replies[0]['text']


def test_unlisted_member_cannot_add_sources(db):
    bot = group_bot(db)
    asyncio.run(bot._handle_update({'message':{'chat':{'id':-12345},'from':{'id':99},'text':'/addchat @example_chat'}}))
    bot.chat_manager.submit.assert_not_called()
    assert not db.is_operator(-12345,99)


@pytest.mark.parametrize('status,fields,expected', [
    ('member',{},True), ('restricted',{'is_member':True},True),
    ('restricted',{'is_member':False},False), ('restricted',{},False),
    ('left',{},False), ('kicked',{},False),
])
def test_operator_must_still_belong_to_configured_group(db,status,fields,expected):
    db.grant_operator(-12345,42)
    bot = group_bot(db,status,**fields)
    assert asyncio.run(bot._authorized({'id':42},-12345)) is expected


def test_allowlist_does_not_apply_to_other_chats_private_dialogs_or_bots(db):
    db.grant_operator(-12345,42)
    bot = group_bot(db)
    assert not asyncio.run(bot._authorized({'id':42},-67890))
    assert not asyncio.run(bot._authorized({'id':42},42))
    assert not asyncio.run(bot._authorized({'id':42,'is_bot':True},-12345))
    bot.api.assert_not_awaited()
    other_bot = Notifier('unused',-67890,db,Metrics())
    other_bot.api = AsyncMock(return_value={'status':'member'})
    assert not asyncio.run(other_bot._authorized({'id':42},-67890))


def test_failed_membership_lookup_does_not_grant_operator_access(db):
    db.grant_operator(-12345,42)
    bot = group_bot(db)
    bot.api = AsyncMock(side_effect=BotApiError('temporary error'))
    assert not asyncio.run(bot._authorized({'id':42},-12345))


def test_existing_group_admin_keeps_access_without_allowlist(db):
    bot = group_bot(db,'administrator')
    assert asyncio.run(bot._authorized({'id':42},-12345))
    assert not db.is_operator(-12345,42)


def test_operator_can_use_existing_feedback_buttons(db):
    db.grant_operator(-12345,42)
    bot = group_bot(db)
    db.set_feedback = Mock(return_value=True)
    asyncio.run(bot._handle_update({'callback_query':{'id':'test','from':{'id':42},
        'message':{'chat':{'id':-12345}},'data':'lead:12'}}))
    db.set_feedback.assert_called_once_with(12,'confirmed')


def test_callback_without_chat_cannot_assume_operator_group(db):
    db.grant_operator(-12345,42)
    bot = group_bot(db)
    db.set_feedback = Mock()
    asyncio.run(bot._handle_update({'callback_query':{'id':'test','from':{'id':42},'data':'lead:12'}}))
    db.set_feedback.assert_not_called()


def test_operator_permission_survives_reopening_and_duplicate_grant(tmp_path):
    path = tmp_path / 'operators.db'
    db = Database(path)
    db.grant_operator(-12345,42,'Test operator')
    db.grant_operator(-12345,42)
    db.close()
    db = Database(path)
    try:
        assert db.is_operator(-12345,42)
        row = db.conn.execute('SELECT COUNT(*) count,display_name FROM bot_operators').fetchone()
        assert row['count'] == 1 and row['display_name'] == 'Test operator'
    finally:
        db.close()
