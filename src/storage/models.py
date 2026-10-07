from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

@dataclass
class IncomingMessage:
    chat_id: int
    chat_title: str
    telegram_message_id: int
    user_id: int | None
    username: str | None
    first_name: str | None
    text: str
    date: datetime
    message_url: str | None
    is_bot: bool = False

@dataclass
class LeadRecord:
    lead_id: int
    message_id: int
    message: IncomingMessage
    lead_type: str
    source_type: str
    product: str | None
    location: str | None
    amount: str | None
    size: str | None
    score: int
    explanation: str
    found: list[tuple[str, str]]
