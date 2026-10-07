"""Main process."""
from __future__ import annotations
import asyncio
import logging
import signal
import sys
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

def merge_runtime_sources(config_chats, runtime_chats):
    merged = {}
    for c in runtime_chats:
        key = str(c.chat_id) if c.chat_id is not None else f"u:{c.username}"
        merged[key] = c
    for c in config_chats:
        key = str(c.chat_id) if c.chat_id is not None else f"u:{c.username}"
        merged[key] = c
    return list(merged.values())

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

    listener = Listener(client, cfg.enabled_chats(), pipeline.handle)
    available = await listener.resolve_chats()
    if not available:
        log.error("No available chats.")
        sys.exit(2)

    for chat in available:
        pipeline.register_chat(chat)
    db.upsert_sources(available)
    listener.register()

    manager = ChatManager(client, db, listener, pipeline, cfg.source_types)
    notifier.set_chat_manager(manager)

    log.info("Monitoring %d chats. Commands: /stats /stats7 /stats30 /addchat", len(available))

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
        asyncio.create_task(metrics_loop(metrics), name="metrics"),
        asyncio.create_task(client.run_until_disconnected(), name="telethon"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
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
