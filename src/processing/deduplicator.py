"""Дедупликация: точный hash + fuzzy-похожесть с недавними лидами."""
from __future__ import annotations
from datetime import datetime

from dataclasses import dataclass

from src.config.loader import DedupSettings
from src.storage.database import Database

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None


@dataclass(frozen=True)
class DuplicateInfo:
    reason: str            # hash | fuzzy | same_user_fuzzy
    original_message_id: int
    similarity: float


class Deduplicator:
    def __init__(self, db: Database, settings: DedupSettings):
        self.db = db
        self.s = settings

    def find_duplicate(
            self,
            text_hash: str,
            normalized: str,
            user_id: int | None,
            reference_date: datetime | None = None,
    ) -> DuplicateInfo | None:
        exact = self.db.find_message_by_hash(
            text_hash,
            self.s.window_days,
            reference_date,
        )

        if exact is not None:
            return DuplicateInfo("hash", exact, 100.0)

        if fuzz is None:
            return None

        for msg_id, other_user, other_text in self.db.recent_lead_texts(
                self.s.window_days,
                self.s.max_candidates,
                reference_date,
        ):
            sim = fuzz.ratio(normalized, other_text)

            if sim >= self.s.fuzzy_threshold:
                return DuplicateInfo("fuzzy", msg_id, sim)

            if (
                    user_id
                    and other_user == user_id
                    and sim >= self.s.same_user_fuzzy_threshold
            ):
                return DuplicateInfo("same_user_fuzzy", msg_id, sim)

        return None