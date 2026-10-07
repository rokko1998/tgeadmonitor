"""Message processing pipeline."""
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
    def __init__(self, cfg: AppConfig, db: Database, notifier: Notifier | None,
                 metrics: Metrics, store_all: bool = True):
        self.cfg, self.db, self.notifier, self.metrics = cfg, db, notifier, metrics
        self.store_all = store_all
        self.classifier = Classifier(cfg)
        self.dedup = Deduplicator(db, cfg.scoring.dedup)
        self.chats = {c.chat_id: c for c in cfg.enabled_chats() if c.chat_id is not None}

    def register_chat(self, chat: Chat) -> None:
        if chat.chat_id is not None:
            self.chats[chat.chat_id] = chat

    def handle(self, msg: IncomingMessage) -> None:
        self.metrics.inc("messages_seen")
        chat = self.chats.get(msg.chat_id)
        if chat is None:
            return
        if msg.is_bot or not msg.text or not msg.text.strip():
            self.metrics.inc("messages_filtered")
            return
        if self.db.message_exists(msg.chat_id, msg.telegram_message_id):
            return

        self.metrics.inc("messages_processed")
        c = self.classifier.classify(msg.text, chat)
        v = c.verdict
        status = {
            "lead": "lead",
            "project_signal": "project_signal",
            "potential": "potential",
            "ignore": "ignored",
        }[v.decision]
        if c.hard_excluded:
            status = "excluded"

        # Federal/professional sources must mention our service geography explicitly.
        if chat.require_location and not c.matches.location:
            if status in ("lead", "project_signal"):
                status = "potential"

        if status in ("ignored", "excluded") and not self.store_all:
            self.metrics.inc("messages_filtered")
            return

        dup = None
        if status in ("lead", "project_signal"):
            dup = self.dedup.find_duplicate(c.hash, c.normalized, msg.user_id, msg.date)
            if dup:
                status = "duplicate"

        message_id = self.db.insert_message(
            telegram_message_id=msg.telegram_message_id, chat_id=msg.chat_id,
            user_id=msg.user_id, username=msg.username, first_name=msg.first_name,
            original_text=msg.text, normalized_text=c.normalized, text_hash=c.hash,
            date=msg.date, message_url=msg.message_url, score=v.score,
            status=status, explanation=v.explain(),
        )
        weights = self.cfg.scoring.weights
        self.db.insert_matches(
            message_id,
            [(m.rule, m.feature, m.text, weights.get(m.feature, 0)) for m in c.matches.matches],
        )

        if status == "duplicate":
            self.metrics.inc("duplicates")
            return
        if status not in ("lead", "project_signal"):
            self.metrics.inc("messages_filtered")
            return

        self.metrics.inc("leads_detected" if status == "lead" else "project_signals_detected")
        r = c.matches
        lead_id = self.db.insert_lead(
            message_id=message_id, lead_type=status, source_type=chat.source_type,
            product=", ".join(r.products) or None, location=r.location,
            amount=", ".join(r.amounts) or None, size=", ".join(r.sizes) or None,
        )
        found = [
            (m.feature, m.text) for m in r.matches
            if m.feature in (
                "intent", "product", "size", "amount", "location", "context",
                "project_signal", "professional_actor",
            )
        ]
        if r.location_from_chat:
            found.append(("location", f"{r.location} (из чата)"))

        lead = LeadRecord(
            lead_id=lead_id, message_id=message_id, message=msg,
            lead_type=status, source_type=chat.source_type,
            product=", ".join(r.products) or None, location=r.location,
            amount=", ".join(r.amounts) or None, size=", ".join(r.sizes) or None,
            score=v.score, explanation=v.explain(), found=found,
        )
        self.db.enqueue_notification(lead_id, build_notification(lead, chat.name))
        if self.notifier:
            self.notifier.wakeup()
