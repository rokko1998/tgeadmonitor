"""Persist incoming text, reject obvious noise, enqueue bounded semantic work."""
from __future__ import annotations

from src.config.loader import AppConfig, Chat
from src.metrics import Metrics
from src.processing.matcher import Matcher
from src.processing.extractor import extract_surface
from src.processing.normalizer import normalize
from src.processing.prefilter import VERSION, prefilter, semantic_text_hash
from src.storage.database import Database
from src.storage.models import IncomingMessage
from src.telegram.notifier import Notifier


class Pipeline:
    def __init__(self, cfg: AppConfig, db: Database, notifier: Notifier | None,
                 metrics: Metrics, store_all: bool = True, archive_only: bool = False):
        self.cfg,self.db,self.notifier,self.metrics=cfg,db,notifier,metrics
        self.store_all=store_all
        self.matcher = Matcher(cfg)
        self.archive_only = archive_only
        self.chats={c.chat_id:c for c in cfg.enabled_chats() if c.chat_id is not None}

    def register_chat(self, chat: Chat) -> None:
        if chat.chat_id is not None:self.chats[chat.chat_id]=chat

    def handle(self, msg: IncomingMessage) -> None:
        self.metrics.inc("messages_seen")
        chat=self.chats.get(msg.chat_id)
        if chat is None or msg.is_bot or not msg.text or not msg.text.strip():return
        if self.db.message_exists(msg.chat_id,msg.telegram_message_id):return
        source=self.db.source(msg.chat_id)
        if source is None or not source["enabled"]:return
        self.metrics.inc("messages_processed")
        archived=self.archive_only or not source["analysis_enabled"]
        reason="source_paused_archive" if archived else prefilter(msg.text)
        if reason and not archived and not self.store_all:
            self.metrics.inc("messages_filtered");return
        normalized=normalize(msg.text)
        meta={}
        if not reason:
            # Regexes extract display metadata; Jev owns classification.
            r = self.matcher.match(normalized, msg.text, extract_surface(msg.text, normalized), chat)
            meta={"product":", ".join(r.products) or None,"location":r.location,
                  "amount":", ".join(r.amounts) or None,"size":", ".join(r.sizes) or None}
        message_id=self.db.insert_message(
            telegram_message_id=msg.telegram_message_id,chat_id=msg.chat_id,user_id=msg.user_id,
            username=msg.username,first_name=msg.first_name,original_text=msg.text,
            normalized_text=normalized,text_hash=semantic_text_hash(msg.text),date=msg.date,
            message_url=msg.message_url,score=0,
            status="archived" if archived else "prefiltered" if reason else "semantic_pending",
            explanation=f"{VERSION}: {reason or 'queued_for_jev'}")
        if reason:
            self.metrics.inc("messages_archived" if archived else "prefilter_rejected")
            return
        self.db.queue_semantic(message_id,meta)
        self.metrics.inc("semantic_queued")
