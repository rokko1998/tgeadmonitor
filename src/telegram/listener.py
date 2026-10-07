"""MTProto listener with runtime source activation."""
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
    return f"https://t.me/c/{s[4:]}/{message_id}" if s.startswith("-100") else None

class Listener:
    def __init__(self, client: TelegramClient, chats: list[Chat],
                 on_message: Callable):
        self.client, self.chats, self.on_message = client, chats, on_message
        self.resolved: dict[int, Chat] = {}
        self._registered = False

    async def resolve_chat(self, chat: Chat) -> Chat | None:
        ref = chat.username if chat.username else chat.chat_id
        try:
            entity = await self.client.get_entity(ref)
        except (RPCError, ValueError) as e:
            log.error("SOURCE_UNAVAILABLE %s (%s): %s", chat.id, ref, e)
            return None
        chat.chat_id = utils.get_peer_id(entity)
        if not chat.username:
            chat.username = getattr(entity, "username", None)
        self.resolved[chat.chat_id] = chat
        log.info("SOURCE_OK %s -> %s type=%s", chat.id, chat.chat_id, chat.source_type)
        return chat

    async def resolve_chats(self) -> list[Chat]:
        ok = []
        for chat in self.chats:
            resolved = await self.resolve_chat(chat)
            if resolved:
                ok.append(resolved)
        return ok

    async def add_chat(self, chat: Chat) -> Chat:
        resolved = await self.resolve_chat(chat)
        if not resolved:
            raise ValueError(f"Could not resolve {chat.id}")
        if all(c.chat_id != resolved.chat_id for c in self.chats):
            self.chats.append(resolved)
        return resolved

    def register(self) -> None:
        if self._registered:
            return
        self._registered = True
        from src.storage.models import IncomingMessage

        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event) -> None:
            try:
                chat = self.resolved.get(event.chat_id)
                if chat is None:
                    return
                text = event.message.message
                if not text:
                    return
                sender = await event.get_sender()
                date = event.message.date
                if date and date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                msg = IncomingMessage(
                    chat_id=event.chat_id, chat_title=chat.name,
                    telegram_message_id=event.message.id,
                    user_id=getattr(sender, "id", None),
                    username=getattr(sender, "username", None),
                    first_name=getattr(sender, "first_name", None) or getattr(sender, "title", None),
                    text=text, date=date,
                    message_url=message_url(chat.username, event.chat_id, event.message.id),
                    is_bot=bool(getattr(sender, "bot", False)),
                )
                result = self.on_message(msg)
                if result is not None:
                    await result
            except Exception:
                log.exception("Handler error chat_id=%s", event.chat_id)
