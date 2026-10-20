"""Persistent, sequential source onboarding using the monitor's MTProto account."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Awaitable, Callable

from telethon import TelegramClient, utils
from telethon.errors import (
    FloodWaitError, InviteHashExpiredError, InviteHashInvalidError,
    InviteRequestSentError, RPCError, UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, Chat as TelegramChat, ChatInviteAlready

from src.config.loader import Chat
from src.processing.pipeline import Pipeline
from src.storage.database import Database
from src.telegram.chat_links import ChatLink, parse_link
from src.telegram.listener import Listener

log = logging.getLogger("chat_manager")


class ApprovalPending(Exception):
    pass


class ChatManager:
    def __init__(self, client: TelegramClient, db: Database, listener: Listener,
                 pipeline: Pipeline, source_types: tuple[str, ...]):
        self.client, self.db, self.listener, self.pipeline = client, db, listener, pipeline
        self.source_types = source_types
        self._wakeup = asyncio.Event()

    def submit(self, raw: str, *, notify: bool = True) -> str:
        link = parse_link(raw)
        if link.kind == "public" and any(
            not c.enabled and (c.username or "").lower() == link.target for c in self.pipeline.cfg.chats
        ):
            raise ValueError("Источник отключён в конфигурации.")
        if link.kind == "public" and any(
            (c.username or "").lower() == link.target for c in self.listener.resolved.values()
        ):
            return "уже мониторится"
        old = self.db.source_request(link.key)
        if old and old["chat_id"] in self.listener.resolved:
            return "уже мониторится"
        added = self.db.enqueue_source(link.key, link.url, notify)
        self._wakeup.set()
        if added:
            return "в очереди на добавление"
        return "заявка ожидает одобрения" if old["status"] == "pending" else "уже в очереди"

    async def _entity(self, link: ChatLink, *, pending: bool = False):
        if link.kind == "public":
            entity = await self.client.get_entity(link.target)
            self._check_entity(entity)
            if isinstance(entity, Channel) and entity.left:
                if pending:
                    raise ApprovalPending
                try:
                    await self.client(JoinChannelRequest(entity))
                except UserAlreadyParticipantError:
                    pass
                entity = await self.client.get_entity(link.target)
                if entity.left:
                    raise ValueError("Участие в чате не подтверждено Telegram.")
            return entity

        checked = await self.client(CheckChatInviteRequest(link.target))
        if isinstance(checked, ChatInviteAlready):
            return checked.chat
        if pending:
            raise ApprovalPending
        try:
            result = await self.client(ImportChatInviteRequest(link.target))
        except UserAlreadyParticipantError:
            result = await self.client(CheckChatInviteRequest(link.target))
            if isinstance(result, ChatInviteAlready):
                return result.chat
            raise ValueError("Не удалось подтвердить участие в закрытом чате.")
        updates = getattr(result, "updates", None)
        chats = getattr(result, "chats", None) or getattr(updates, "chats", None) or []
        if not chats:
            checked = await self.client(CheckChatInviteRequest(link.target))
            if isinstance(checked, ChatInviteAlready):
                return checked.chat
            raise ValueError("Telegram не вернул добавленный чат. Пришли ссылку повторно.")
        if len(chats) != 1:
            checked = await self.client(CheckChatInviteRequest(link.target))
            if isinstance(checked, ChatInviteAlready):
                return checked.chat
            raise ValueError("Telegram вернул несколько чатов. Пришли ссылку повторно.")
        return chats[0]

    @staticmethod
    def _check_entity(entity) -> None:
        if not isinstance(entity, (Channel, TelegramChat)):
            raise ValueError("Ссылка ведёт на пользователя или бота, а нужен чат/канал.")
        if getattr(entity, "deactivated", False):
            raise ValueError("Этот чат больше не активен.")

    def _activate(self, entity) -> tuple[Chat, bool]:
        self._check_entity(entity)
        chat_id = utils.get_peer_id(entity)
        if chat_id in self.listener.resolved:
            return self.listener.resolved[chat_id], False
        username = getattr(entity, "username", None)
        known = next((c for c in self.pipeline.cfg.chats if c.chat_id == chat_id or (
            username and (c.username or "").lower() == username.lower()
        )), None)
        if known and not known.enabled:
            raise ValueError("Источник отключён в конфигурации. Сначала включи его в config.")
        chat = replace(known) if known else Chat(
            id=username.lower() if username else f"chat_{abs(chat_id)}",
            name=getattr(entity, "title", None) or username or str(chat_id),
            source_type="unclassified", priority=2,
        )
        chat.chat_id, chat.username = chat_id, username
        chat.name = getattr(entity, "title", None) or chat.name
        chat.require_location = False
        self.db.upsert_source(chat, added_via="config" if known else "telegram_bot")
        self.pipeline.register_chat(chat)
        self.listener.resolved[chat_id] = chat
        return chat, True

    async def process_request(self, row) -> str | None:
        link = parse_link(row["url"])
        key, previous = row["link_key"], row["status"]
        try:
            entity = await self._entity(link, pending=bool(row["approval_requested"]))
            chat, added = self._activate(entity)
        except (ApprovalPending, InviteRequestSentError):
            self.db.finish_source_request(key, "pending", delay=300)
            return "⏳ Заявка на вступление отправлена. После одобрения мониторинг включится автоматически." if not row["approval_requested"] else None
        except FloodWaitError as exc:
            self.listener.retry_after = time.time() + exc.seconds + 1
            self.db.finish_source_request(key, "flood_wait", error="FloodWaitError", delay=exc.seconds + 1)
            return f"⏳ Telegram ограничил запросы на {exc.seconds} сек. Очередь сохранена и продолжится автоматически."
        except (InviteHashExpiredError, InviteHashInvalidError):
            self.db.finish_source_request(key, "failed", error="invalid_invite")
            return "❌ Приглашение недействительно или истекло. Нужна новая ссылка."
        except (OSError, asyncio.TimeoutError):
            self.db.finish_source_request(key, "retry", error="network", delay=60)
            return "⏳ Ошибка связи с Telegram. Повторю автоматически." if previous != "retry" else None
        except (RPCError, ValueError) as exc:
            self.db.finish_source_request(key, "failed", error=type(exc).__name__)
            if isinstance(exc, ValueError):
                return f"❌ {exc}"
            return f"❌ Telegram отклонил добавление ({type(exc).__name__}). Проверь ссылку и доступ аккаунта."
        self.db.finish_source_request(key, "active", chat_id=chat.chat_id)
        return f"✅ {chat.name}: {'добавлен, мониторинг включён' if added else 'уже мониторится'}."

    async def run(self, report: Callable[[str], Awaitable[None]]) -> None:
        while True:
            self._wakeup.clear()
            cooldown = max(self.db.source_cooldown_until(), self.listener.retry_after) - time.time()
            row = self.db.next_source_request() if cooldown <= 0 else None
            if row is None:
                timeout = min(30, max(1, cooldown)) if cooldown > 0 else 30
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                result = await self.process_request(row)
            except Exception as exc:
                log.error("Source onboarding failed: %s (link omitted)", type(exc).__name__)
                self.db.finish_source_request(row["link_key"], "retry", error="internal", delay=60)
                result = "⏳ Ошибка добавления. Очередь сохранена; повторю автоматически."
            if result:
                if row["notify"]:
                    try:
                        await report(f"{row['url']}\n{result}")
                    except Exception:
                        log.warning("Could not send source onboarding result")
                else:
                    log.info("Configured source onboarding: %s", result)
            await asyncio.sleep(3)
