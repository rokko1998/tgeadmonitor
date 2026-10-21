"""Explicit historical import into the archive; never pays Jev or sends cards."""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.config.loader import load_config
from src.metrics import Metrics
from src.processing.pipeline import Pipeline
from src.settings import Settings
from src.storage.database import Database
from src.storage.models import IncomingMessage
from src.telegram.listener import Listener, build_client, message_url


async def run_backfill(days: int) -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level)
    db = Database(settings.db_path)
    client = build_client(settings.api_id, settings.api_hash, settings.session, settings.data_dir)
    try:
        db.seed_sources_once()
        cfg = load_config(settings.config_dir)
        cfg.chats = db.runtime_sources()
        pipeline = Pipeline(cfg, db, None, Metrics(), archive_only=True)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram account is not authorized")
        await client.get_dialogs()
        listener = Listener(client, cfg.enabled_chats(), pipeline.handle)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        for chat in await listener.resolve_chats():
            db.upsert_source(chat)
            pipeline.register_chat(chat)
            count = 0
            async for msg in client.iter_messages(chat.chat_id, offset_date=since, reverse=True):
                if not msg.message or not msg.date or msg.date < since:
                    continue
                try:
                    sender = await msg.get_sender()
                except Exception:
                    sender = None
                pipeline.handle(IncomingMessage(
                    chat_id=chat.chat_id, chat_title=chat.name,
                    telegram_message_id=msg.id, user_id=getattr(sender, "id", None),
                    username=getattr(sender, "username", None), first_name=getattr(sender, "first_name", None),
                    text=msg.message, date=msg.date,
                    message_url=message_url(chat.username, chat.chat_id, msg.id),
                    is_bot=bool(getattr(sender, "bot", False)),
                ))
                count += 1
            logging.info("Archived history source=%s messages=%s", chat.id, count)
    finally:
        await client.disconnect()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive Telegram history without Jev or notifications")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    asyncio.run(run_backfill(args.days))


if __name__ == "__main__":
    main()
