"""Main process."""
from __future__ import annotations
import asyncio
import logging
import signal
import sys
import time
from dataclasses import replace
from telethon.errors import FloodWaitError, RPCError
from src.config.loader import ConfigError, load_config
from src.metrics import Metrics
from src.processing.pipeline import Pipeline
from src.settings import Settings, SettingsError
from src.storage.database import Database
from src.telegram.chat_manager import ChatManager
from src.telegram.listener import Listener, build_client
from src.telegram.notifier import Notifier

log = logging.getLogger("main")

async def metrics_loop(metrics: Metrics, interval: int = 3600) -> None:
    while True:
        await asyncio.sleep(interval)
        metrics.log_summary()

async def restore_sources(listener: Listener, pipeline: Pipeline, db: Database) -> None:
    """Retry unavailable sources after temporary startup errors, without joining them."""
    while True:
        await asyncio.sleep(60)
        if max(listener.retry_after, db.source_cooldown_until()) > time.time():
            continue
        if any(c.chat_id not in listener.resolved and not c.username for c in listener.chats):
            try:
                await listener.client.get_dialogs()
            except FloodWaitError as exc:
                listener.retry_after = time.time() + exc.seconds + 1
                continue
            except (RPCError, OSError):
                continue
        for chat in await listener.resolve_chats():
            pipeline.register_chat(chat)
            db.upsert_source(chat)

def merge_runtime_sources(config_chats, runtime_chats):
    merged = list(runtime_chats)
    for chat in config_chats:
        matches = [c for c in merged if c.id == chat.id or (
            chat.chat_id is not None and c.chat_id == chat.chat_id
        ) or (chat.username and c.username and chat.username.lower() == c.username.lower())]
        if matches:
            known_id = next((c.chat_id for c in matches if c.chat_id is not None), None)
            chat = replace(chat, chat_id=chat.chat_id if chat.chat_id is not None else known_id)
            merged = [c for c in merged if c not in matches]
        merged.append(chat)
    return merged

async def run() -> None:
    try:
        settings = Settings.from_env()
    except SettingsError as e:
        print(f"Settings error: {e}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    try:
        cfg = load_config(settings.config_dir)
    except ConfigError as e:
        log.error("Config error: %s", e)
        sys.exit(2)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    cfg.chats = merge_runtime_sources(cfg.chats, db.runtime_sources())

    metrics = Metrics()
    notifier = Notifier(settings.bot_token, settings.owner_chat_id, db, metrics)
    await notifier.start()
    pipeline = Pipeline(cfg, db, notifier, metrics, store_all=settings.store_all_messages)

    client = build_client(settings.api_id, settings.api_hash, settings.session, settings.data_dir)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Telegram account is not authorized.")
        sys.exit(2)

    # StringSession does not retain the entity cache between container rebuilds.
    # Loading dialogs restores access hashes needed to resolve private chat IDs.
    startup_retry_after = db.source_cooldown_until()
    if startup_retry_after <= time.time():
        try:
            await client.get_dialogs()
        except FloodWaitError as exc:
            startup_retry_after = time.time() + exc.seconds + 1
            log.warning("Dialog preload limited for %s seconds", exc.seconds)
        except RPCError as exc:
            log.warning("Could not preload dialogs: %s", type(exc).__name__)

    listener = Listener(client, cfg.enabled_chats(), pipeline.handle)
    listener.retry_after = startup_retry_after
    available = await listener.resolve_chats()
    if not available:
        log.warning("No available chats yet. Add sources by sending links to the bot.")

    for chat in available:
        pipeline.register_chat(chat)
    db.upsert_sources(available)
    listener.register()

    manager = ChatManager(client, db, listener, pipeline, cfg.source_types)
    notifier.set_chat_manager(manager)
    for chat in cfg.enabled_chats():
        if chat.auto_join and chat.username:
            manager.submit(f"@{chat.username}", notify=False)

    log.info("Monitoring %d chats. Commands: /stats /stats7 /stats30 /addchat /chats", len(available))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(notifier.run_sender(), name="sender"),
        asyncio.create_task(notifier.run_polling(), name="polling"),
        asyncio.create_task(manager.run(notifier.send_owner), name="source_onboarding"),
        asyncio.create_task(restore_sources(listener, pipeline, db), name="source_restore"),
        asyncio.create_task(metrics_loop(metrics), name="metrics"),
        asyncio.create_task(client.run_until_disconnected(), name="telethon"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        if not task.cancelled() and task.exception():
            log.error("Task %s failed: %s", task.get_name(), task.exception())
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    await client.disconnect()
    await notifier.close()
    db.close()

def main() -> None:
    while True:
        try:
            asyncio.run(run())
            return
        except SystemExit:
            raise
        except Exception:
            log.exception("Fatal error, restarting in 15s")
            import time
            time.sleep(15)

if __name__ == "__main__":
    main()
