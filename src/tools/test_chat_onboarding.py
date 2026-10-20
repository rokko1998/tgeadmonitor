"""Offline regression coverage for source onboarding. No real Telegram connections."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.errors import ChannelPrivateError, InviteRequestSentError
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, ChatPhotoEmpty, User

from src.config.loader import Chat
from src.metrics import Metrics
from src.storage.database import Database
from src.telegram.chat_links import extract_links, parse_link
from src.telegram.chat_manager import ChatManager
from src.telegram.notifier import Notifier


@pytest.mark.parametrize("value", [
    "@Example_chat", "https://t.me/Example_chat", "t.me/example_chat/123?single",
    "https://telegram.me/example_chat/", "tg://resolve?domain=Example_chat",
])
def test_public_references_have_one_identity(value):
    assert parse_link(value).key == "public:example_chat"


def test_private_invites_preserve_case():
    assert parse_link("https://t.me/+AbC_123").key == "invite:AbC_123"
    assert parse_link("https://t.me/joinchat/AbC_123").key == "invite:AbC_123"


@pytest.mark.parametrize("value", [
    "https://example.com/t.me/example_chat", "https://t.me/c/123/45",
    "https://t.me/share/url?url=example", "https://t.me/example_chat/other",
])
def test_non_chat_references_are_rejected(value):
    with pytest.raises(ValueError):
        parse_link(value)


def test_links_are_extracted_from_a_message():
    assert extract_links("Чаты: https://t.me/example_chat, @another_chat\nhttps://t.me/+AbC_123.") == [
        "https://t.me/example_chat", "@another_chat", "https://t.me/+AbC_123",
    ]


def test_queue_and_runtime_sources_survive_reopening(tmp_path):
    path = tmp_path / "leads.db"
    db = Database(path)
    assert db.enqueue_source("public:example_chat", "https://t.me/example_chat")
    assert not db.enqueue_source("public:example_chat", "https://t.me/example_chat")
    db.finish_source_request("public:example_chat", "pending", delay=300)
    db.upsert_source(Chat(id="runtime", name="Runtime", chat_id=-100123, source_type="unclassified"),
                     added_via="telegram_bot")
    db.close()
    db = Database(path)
    try:
        assert db.source_request("public:example_chat")["approval_requested"] == 1
        assert db.runtime_sources()[0].chat_id == -100123
    finally:
        db.close()


def make_manager(db, entity):
    client = AsyncMock()
    client.get_entity.return_value = entity
    listener = SimpleNamespace(resolved={}, retry_after=0)
    pipeline = SimpleNamespace(cfg=SimpleNamespace(chats=[]), register_chat=Mock())
    return ChatManager(client, db, listener, pipeline, ("unclassified",))


def channel(left=False):
    return Channel(id=123, title="Example", photo=ChatPhotoEmpty(),
                   date=datetime.now(timezone.utc), username="example_chat", left=left, megagroup=True)


def test_join_failure_never_activates_source(tmp_path):
    db = Database(tmp_path / "leads.db")
    try:
        manager = make_manager(db, channel(left=True))
        manager.client.side_effect = ChannelPrivateError(request=None)
        manager.submit("@example_chat")
        result = asyncio.run(manager.process_request(db.next_source_request()))
        assert "❌" in result
        assert not manager.listener.resolved
        assert not db.runtime_sources()
    finally:
        db.close()


def test_bot_or_user_is_not_a_source(tmp_path):
    db = Database(tmp_path / "leads.db")
    try:
        manager = make_manager(db, User(id=1, bot=True))
        manager.submit("@example_bot")
        result = asyncio.run(manager.process_request(db.next_source_request()))
        assert "пользователя или бота" in result
        assert not manager.listener.resolved
    finally:
        db.close()


def test_success_persists_and_activates_once(tmp_path):
    db = Database(tmp_path / "leads.db")
    try:
        manager = make_manager(db, channel())
        manager.submit("@example_chat")
        result = asyncio.run(manager.process_request(db.next_source_request()))
        assert "мониторинг включён" in result
        assert len(manager.listener.resolved) == 1
        assert len(db.runtime_sources()) == 1
        assert db.source_request("public:example_chat")["status"] == "active"
        assert manager.submit("https://t.me/Example_chat") == "уже мониторится"
    finally:
        db.close()


def test_pending_approval_checks_without_rejoining(tmp_path):
    db = Database(tmp_path / "leads.db")
    try:
        manager = make_manager(db, None)
        manager.submit("https://t.me/+AbC_123")
        manager.client.side_effect = InviteRequestSentError(request=None)
        asyncio.run(manager.process_request(db.next_source_request()))
        manager.client.reset_mock(side_effect=True)
        manager.client.return_value = SimpleNamespace()
        row = db.source_request("invite:AbC_123")
        assert asyncio.run(manager.process_request(row)) is None
        assert manager.client.await_count == 1
        assert isinstance(manager.client.await_args.args[0], CheckChatInviteRequest)
        assert not manager.listener.resolved
    finally:
        db.close()


def test_owner_can_submit_batch_and_unauthorized_sender_cannot(tmp_path):
    db = Database(tmp_path / "leads.db")
    try:
        notifier = Notifier("unused", 42, db, Metrics())
        notifier.api = AsyncMock()
        manager = Mock()
        manager.submit.return_value = "в очереди на добавление"
        notifier.set_chat_manager(manager)
        update = {"message": {"chat": {"id": 42}, "from": {"id": 99},
                              "text": "@example_chat @Example_chat @another_chat"}}
        asyncio.run(notifier._handle_update(update))
        manager.submit.assert_not_called()
        update["message"]["from"]["id"] = 42
        asyncio.run(notifier._handle_update(update))
        assert manager.submit.call_count == 2
    finally:
        db.close()


def test_one_invalid_link_does_not_cancel_other_links(tmp_path):
    db = Database(tmp_path / "leads.db")
    try:
        notifier = Notifier("unused", 42, db, Metrics())
        notifier.api = AsyncMock()
        manager = Mock()
        manager.submit.side_effect = ["в очереди", ValueError("Ошибка ссылки"), "в очереди"]
        notifier.set_chat_manager(manager)
        asyncio.run(notifier._add_links("@first_chat https://t.me/+AbC_123 @last_chat"))
        assert manager.submit.call_count == 3
        body = notifier.api.await_args.kwargs["text"]
        assert "first_chat" in body and "last_chat" in body and "Ошибка ссылки" in body
    finally:
        db.close()
