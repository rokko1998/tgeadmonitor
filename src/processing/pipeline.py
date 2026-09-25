"""Оркестрация обработки одного сообщения: валидация источника -> классификация -> дедуп -> БД -> очередь."""
from __future__ import annotations

import logging

from src.config.loader import AppConfig, Chat
from src.metrics import Metrics
from src.processing.classifier import Classifier
from src.processing.deduplicator import Deduplicator
from src.storage.database import Database
from src.storage.models import IncomingMessage, LeadRecord
from src.telegram.formatter import build_notification
from src.telegram.notifier import Notifier

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(self, cfg: AppConfig, db: Database, notifier: Notifier | None, metrics: Metrics,
                 store_all: bool = True):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.metrics = metrics
        self.store_all = store_all
        self.classifier = Classifier(cfg)
        self.dedup = Deduplicator(db, cfg.scoring.dedup)
        self.chats: dict[int, Chat] = {c.chat_id: c for c in cfg.enabled_chats() if c.chat_id is not None}

    def register_chat(self, chat: Chat) -> None:
        if chat.chat_id is not None:
            self.chats[chat.chat_id] = chat

    def handle(self, msg: IncomingMessage) -> None:
        self.metrics.inc("messages_seen")
        chat = self.chats.get(msg.chat_id)
        if chat is None:
            log.debug("MESSAGE_IGNORED unknown source chat_id=%s", msg.chat_id)
            return
        if msg.is_bot or not msg.text or not msg.text.strip():
            self.metrics.inc("messages_filtered")
            return
        if self.db.message_exists(msg.chat_id, msg.telegram_message_id):
            return

        log.info("MESSAGE_RECEIVED chat=%s msg=%s user=%s", chat.id, msg.telegram_message_id, msg.user_id)
        self.metrics.inc("messages_processed")

        c = self.classifier.classify(msg.text, chat)
        v = c.verdict
        status = {"lead": "lead", "potential": "potential", "ignore": "ignored"}[v.decision]
        if c.hard_excluded:
            status = "excluded"

        if status in ("ignored", "excluded") and not self.store_all:
            self.metrics.inc("messages_filtered")
            log.info("MESSAGE_IGNORED chat=%s msg=%s score=%s", chat.id, msg.telegram_message_id, v.score)
            return

        # дедупликация (только для лидов — потенциальные и мусор не уведомляются)
        dup = None
        if status == "lead":
            dup = self.dedup.find_duplicate(
                c.hash,
                c.normalized,
                msg.user_id,
                msg.date,
            )
            if dup:
                status = "duplicate"

        message_id = self.db.insert_message(
            telegram_message_id=msg.telegram_message_id, chat_id=msg.chat_id, user_id=msg.user_id,
            username=msg.username, first_name=msg.first_name, original_text=msg.text,
            normalized_text=c.normalized, text_hash=c.hash, date=msg.date, message_url=msg.message_url,
            score=v.score, status=status, explanation=v.explain(),
        )
        weights = self.cfg.scoring.weights
        self.db.insert_matches(message_id, [(m.rule, m.feature, m.text, weights.get(m.feature, 0))
                                            for m in c.matches.matches])

        if status == "duplicate":
            self.metrics.inc("duplicates")
            log.info("DUPLICATE_SKIPPED chat=%s msg=%s reason=%s of=%s sim=%.0f",
                     chat.id, msg.telegram_message_id, dup.reason, dup.original_message_id, dup.similarity)
            return
        if status != "lead":
            self.metrics.inc("messages_filtered")
            log.info("MESSAGE_IGNORED chat=%s msg=%s status=%s score=%s", chat.id, msg.telegram_message_id,
                     status, v.score)
            return

        self.metrics.inc("leads_detected")
        log.info("MESSAGE_MATCHED chat=%s msg=%s\n%s", chat.id, msg.telegram_message_id, v.explain())

        r = c.matches
        lead_id = self.db.insert_lead(
            message_id,
            product=", ".join(r.products) or None,
            location=r.location,
            amount=", ".join(r.amounts) or None,
            size=", ".join(r.sizes) or None,
        )
        found = [(m.feature, m.text) for m in r.matches if m.feature in
                 ("intent", "product", "size", "amount", "location", "context")]
        if r.location_from_chat:
            found.append(("location", f"{r.location} (из чата)"))
        lead = LeadRecord(lead_id=lead_id, message_id=message_id, message=msg,
                          product=", ".join(r.products) or None, location=r.location,
                          amount=", ".join(r.amounts) or None, size=", ".join(r.sizes) or None,
                          score=v.score, explanation=v.explain(), found=found)
        log.info("LEAD_CREATED lead_id=%s product=%s location=%s score=%s", lead_id, lead.product,
                 lead.location, v.score)

        payload = build_notification(lead, chat.name)
        self.db.enqueue_notification(lead_id, payload)
        if self.notifier:
            self.notifier.wakeup()