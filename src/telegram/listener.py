"""Чтение сообщений из чатов через Telethon (MTProto, аккаунт пользователя)."""
from __future__ import annotations

import logging
from datetime import timezone
from typing import Awaitable, Callable

from telethon import TelegramClient, events, utils
from telethon.errors import RPCError
from telethon.sessions import StringSession

from src.config.loader import Chat

log = logging.getLogger("listener")


def build_client(api_id: int, api_hash: str, session: str, data_dir) -> TelegramClient:
    sess = StringSession(session) if session else str(data_dir / "account")
    return TelegramClient(sess, api_id, api_hash, connection_retries=None, retry_delay=5,
                          auto_reconnect=True, request_retries=5)


def message_url(chat_username: str | None, chat_id: int, message_id: int) -> str | None:
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None


class Listener:
    def __init__(self, client: TelegramClient, chats: list[Chat],
                 on_message: Callable[["IncomingMessage"], Awaitable[None] | None]):
        self.client = client
        self.chats = chats
        self.on_message = on_message
        self.resolved: dict[int, Chat] = {}

    async def resolve_chats(self) -> list[Chat]:
        """Разрешает username -> chat_id, проверяет доступ ко всем чатам. Возвращает доступные."""
        ok: list[Chat] = []
        for chat in self.chats:
            ref = chat.username if chat.username else chat.chat_id
            try:
                entity = await self.client.get_entity(ref)
            except (RPCError, ValueError) as e:
                log.error("SOURCE_UNAVAILABLE %s (%s): %s", chat.id, ref, e)
                continue
            chat.chat_id = utils.get_peer_id(entity)
            if not chat.username:
                chat.username = getattr(entity, "username", None)
            self.resolved[chat.chat_id] = chat
            ok.append(chat)
            log.info("SOURCE_OK %s -> %s (%s)", chat.id, chat.chat_id, getattr(entity, "title", ""))
        return ok

    def register(self) -> None:
        from src.storage.models import IncomingMessage  # локальный импорт, чтобы избежать цикла

        chat_ids = list(self.resolved.keys())

        @self.client.on(events.NewMessage(chats=chat_ids, incoming=True))
        async def handler(event: events.NewMessage.Event) -> None:
            try:
                text = event.message.message  # текст или caption
                if not text:
                    return
                chat = self.resolved.get(event.chat_id)
                if chat is None:
                    return
                sender = await event.get_sender()
                user_id = getattr(sender, "id", None)
                username = getattr(sender, "username", None)
                first_name = getattr(sender, "first_name", None) or getattr(sender, "title", None)
                is_bot = bool(getattr(sender, "bot", False))
                date = event.message.date
                if date and date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                msg = IncomingMessage(
                    chat_id=event.chat_id, chat_title=chat.name,
                    telegram_message_id=event.message.id, user_id=user_id, username=username,
                    first_name=first_name, text=text, date=date,
                    message_url=message_url(chat.username, event.chat_id, event.message.id),
                    is_bot=is_bot,
                )
                result = self.on_message(msg)
                if result is not None:
                    await result
            except Exception:
                # одно кривое сообщение не должно ронять сервис
                log.exception("Handler error chat_id=%s msg=%s", event.chat_id, getattr(event.message, "id", None))