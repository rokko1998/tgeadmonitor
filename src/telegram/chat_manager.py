"""Friendly /addchat backend."""
from __future__ import annotations
import re
from dataclasses import dataclass
from telethon import TelegramClient, utils
from telethon.errors import InviteHashExpiredError, InviteHashInvalidError, InviteRequestSentError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready
from src.config.loader import Chat
from src.processing.pipeline import Pipeline
from src.storage.database import Database
from src.telegram.listener import Listener

PUBLIC_RE = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{5,})/?$")
INVITE_RE = re.compile(r"^(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)/?$")

@dataclass
class ChatCandidate:
    chat: Chat

class ChatManager:
    def __init__(self, client: TelegramClient, db: Database, listener: Listener,
                 pipeline: Pipeline, source_types: tuple[str, ...]):
        self.client, self.db, self.listener, self.pipeline = client, db, listener, pipeline
        self.source_types = source_types

    async def prepare(self, raw_link: str) -> ChatCandidate:
        link = raw_link.strip()
        invite, public = INVITE_RE.match(link), PUBLIC_RE.match(link)

        if invite:
            invite_hash = invite.group(1)
            try:
                checked = await self.client(CheckChatInviteRequest(invite_hash))
            except (InviteHashInvalidError, InviteHashExpiredError) as e:
                raise ValueError("Пригласительная ссылка недействительна или истекла.") from e
            if isinstance(checked, ChatInviteAlready):
                entity = checked.chat
            else:
                try:
                    result = await self.client(ImportChatInviteRequest(invite_hash))
                except InviteRequestSentError as e:
                    raise ValueError("Заявка на вступление отправлена. Повтори /addchat после одобрения.") from e
                if not result.chats:
                    raise ValueError("Telegram не вернул чат.")
                entity = result.chats[0]
        elif public:
            username = public.group(1)
            entity = await self.client.get_entity(username)
            try:
                await self.client(JoinChannelRequest(entity))
            except Exception as e:
                if type(e).__name__ != "UserAlreadyParticipantError":
                    # Some channels are readable without a join; resolution below is the real test.
                    entity = await self.client.get_entity(username)
        else:
            raise ValueError("Нужна ссылка вида https://t.me/name или https://t.me/+invite")

        chat_id = utils.get_peer_id(entity)
        username = getattr(entity, "username", None)
        title = getattr(entity, "title", None) or username or str(chat_id)
        internal_id = username.lower() if username else f"chat_{abs(chat_id)}"
        return ChatCandidate(Chat(
            id=internal_id, name=title, chat_id=chat_id, username=username,
            source_type="unclassified", priority=2, comment="added via Telegram bot",
        ))

    async def activate(self, candidate: ChatCandidate, source_type: str,
                       location: str | None, require_location: bool) -> Chat:
        if source_type not in self.source_types:
            raise ValueError("Неизвестный source_type")
        chat = candidate.chat
        chat.source_type = source_type
        chat.location = location or None
        chat.require_location = require_location
        resolved = await self.listener.add_chat(chat)
        self.pipeline.register_chat(resolved)
        self.db.upsert_source(resolved, added_via="telegram_bot")
        return resolved
