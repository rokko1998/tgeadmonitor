from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from telethon import utils

from src.config.loader import load_config
from src.metrics import Metrics
from src.processing.pipeline import Pipeline
from src.settings import Settings
from src.storage.database import Database
from src.storage.models import IncomingMessage
from src.telegram.listener import build_client, message_url
from src.telegram.notifier import Notifier


log = logging.getLogger("backfill")


async def run_backfill(days: int, notify: bool) -> None:
    settings = Settings.from_env()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(settings.config_dir)

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    metrics = Metrics()

    notifier = None

    if notify:
        notifier = Notifier(
            settings.bot_token,
            settings.owner_chat_id,
            db,
            metrics,
        )
        await notifier.start()

    pipeline = Pipeline(
        cfg,
        db,
        notifier,
        metrics,
        store_all=settings.store_all_messages,
    )

    client = build_client(
        settings.api_id,
        settings.api_hash,
        settings.session,
        settings.data_dir,
    )

    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Telegram account is not authorized")

    since = datetime.now(timezone.utc) - timedelta(days=days)

    stats = defaultdict(
        lambda: {
            "messages": 0,
            "leads": 0,
            "potential": 0,
            "ignored": 0,
        }
    )

    sender_task = None

    if notifier:
        sender_task = asyncio.create_task(notifier.run_sender())

    try:
        for chat in cfg.enabled_chats():
            ref = (
                chat.chat_id
                if chat.chat_id is not None
                else chat.username
            )

            try:
                entity = await client.get_entity(ref)
            except Exception as exc:
                log.error(
                    "SOURCE_UNAVAILABLE %s (%s): %s",
                    chat.id,
                    ref,
                    exc,
                )
                continue

            chat.chat_id = utils.get_peer_id(entity)

            if not chat.username:
                chat.username = getattr(entity, "username", None)

            pipeline.register_chat(chat)
            db.upsert_sources([chat])

            log.info(
                "BACKFILL_SOURCE_START chat=%s since=%s",
                chat.id,
                since.isoformat(),
            )

            # reverse=True => от старых сообщений к новым.
            # Это важно для корректного dedup.
            async for msg in client.iter_messages(
                entity,
                offset_date=since,
                reverse=True,
            ):
                if not msg.date:
                    continue

                if msg.date < since:
                    continue

                text = msg.message

                if not text:
                    continue

                try:
                    sender = await msg.get_sender()
                except Exception:
                    sender = None

                incoming = IncomingMessage(
                    chat_id=chat.chat_id,
                    chat_title=chat.name,
                    telegram_message_id=msg.id,
                    user_id=getattr(sender, "id", None),
                    username=getattr(sender, "username", None),
                    first_name=(
                        getattr(sender, "first_name", None)
                        or getattr(sender, "title", None)
                    ),
                    text=text,
                    date=msg.date,
                    message_url=message_url(
                        chat.username,
                        chat.chat_id,
                        msg.id,
                    ),
                    is_bot=bool(getattr(sender, "bot", False)),
                )

                stats[chat.id]["messages"] += 1

                classification = pipeline.classifier.classify(
                    text,
                    chat,
                )

                if classification.verdict.decision == "lead":
                    stats[chat.id]["leads"] += 1
                elif classification.verdict.decision == "potential":
                    stats[chat.id]["potential"] += 1
                else:
                    stats[chat.id]["ignored"] += 1

                pipeline.handle(incoming)

            current = stats[chat.id]

            log.info(
                "BACKFILL_SOURCE_DONE chat=%s messages=%s "
                "leads=%s potential=%s",
                chat.id,
                current["messages"],
                current["leads"],
                current["potential"],
            )

        if notifier:
            log.info("Waiting for notification queue...")

            while db.pending_notifications():
                await asyncio.sleep(1)

    finally:
        if sender_task:
            sender_task.cancel()

            await asyncio.gather(
                sender_task,
                return_exceptions=True,
            )

        await client.disconnect()

        if notifier:
            await notifier.close()

    print()
    print("=== BACKFILL SUMMARY ===")
    print()

    total_messages = 0
    total_leads = 0
    total_potential = 0

    for chat_id, stat in sorted(
        stats.items(),
        key=lambda item: item[1]["leads"],
        reverse=True,
    ):
        total_messages += stat["messages"]
        total_leads += stat["leads"]
        total_potential += stat["potential"]

        rate = (
            stat["leads"] / stat["messages"] * 100
            if stat["messages"]
            else 0
        )

        print(
            f"{chat_id:30} "
            f"messages={stat['messages']:6} "
            f"leads={stat['leads']:4} "
            f"potential={stat['potential']:4} "
            f"lead_rate={rate:6.2f}%"
        )

    print()
    print(
        f"TOTAL messages={total_messages} "
        f"leads={total_leads} "
        f"potential={total_potential}"
    )

    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Historical Telegram lead scan"
    )

    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="How many days of history to scan",
    )

    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send detected leads through Telegram notifications",
    )

    args = parser.parse_args()

    asyncio.run(
        run_backfill(
            days=args.days,
            notify=args.notify,
        )
    )


if __name__ == "__main__":
    main()