"""Bot API notifier, feedback, analytics and /addchat wizard."""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any
import aiohttp
from src.metrics import Metrics
from src.storage.database import Database

log = logging.getLogger("notifier")
FEEDBACK = {"lead": ("confirmed", "✅ Отмечено как лид"),
            "notlead": ("rejected", "❌ Отмечено: не лид")}

class BotApiError(Exception):
    pass

class Notifier:
    def __init__(self, token: str, owner_chat_id: int, db: Database, metrics: Metrics,
                 retry_interval: float = 5.0, max_attempts: int = 50):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.owner, self.db, self.metrics = owner_chat_id, db, metrics
        self.retry_interval, self.max_attempts = retry_interval, max_attempts
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0
        self._wakeup = asyncio.Event()
        self.chat_manager = None
        self._add_stage = None
        self._candidate = None
        self._source_type = None
        self._location = None

    def set_chat_manager(self, manager) -> None:
        self.chat_manager = manager

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        me = await self.api("getMe")
        log.info("Bot authorized as @%s", me.get("username"))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def wakeup(self) -> None:
        self._wakeup.set()

    async def api(self, method: str, **params: Any) -> Any:
        assert self._session is not None
        payload = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                   for k, v in params.items() if v is not None}
        async with self._session.post(self.base + method, data=payload) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise BotApiError(f"{method}: {data.get('description')} (code {data.get('error_code')})")
        return data["result"]

    async def run_sender(self) -> None:
        while True:
            try:
                for row in self.db.pending_notifications(self.max_attempts):
                    await self._send_row(row)
            except Exception:
                log.exception("Sender loop error")
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.retry_interval)
            except asyncio.TimeoutError:
                pass

    async def _send_row(self, row: Any) -> None:
        payload = json.loads(row["payload"])
        try:
            result = await self.api(
                "sendMessage", chat_id=self.owner, text=payload["text"],
                parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=payload.get("reply_markup"),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, BotApiError) as e:
            final = row["attempts"] + 1 >= self.max_attempts
            self.db.mark_notification_failed(int(row["id"]), str(e), final=final)
            self.metrics.inc("notification_failed")
            return
        self.db.mark_notification_sent(int(row["id"]))
        self.db.set_lead_notified(int(row["lead_id"]), result.get("message_id"))
        self.metrics.inc("notifications_sent")

    async def run_polling(self) -> None:
        while True:
            try:
                updates = await self.api(
                    "getUpdates", offset=self._offset, timeout=50,
                    allowed_updates=["callback_query", "message"],
                )
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    await self._handle_update(upd)
            except (aiohttp.ClientError, asyncio.TimeoutError, BotApiError) as e:
                log.warning("Polling error: %s", e)
                await asyncio.sleep(self.retry_interval)
            except Exception:
                log.exception("Polling loop error")
                await asyncio.sleep(self.retry_interval)

    async def _send_owner(self, text: str) -> None:
        await self.api("sendMessage", chat_id=self.owner, text=text,
                       disable_web_page_preview=True)

    async def _send_stats(self, days: int | None) -> None:
        stats = self.db.stats(days)
        stats.update(self.metrics.snapshot())
        body = json.dumps(stats, ensure_ascii=False, indent=1)
        # Telegram max is 4096; show a compact leading slice.
        await self.api("sendMessage", chat_id=self.owner,
                       text="<pre>" + body[:3900] + "</pre>", parse_mode="HTML")

    def _reset_addchat(self) -> None:
        self._add_stage = self._candidate = self._source_type = self._location = None

    async def _start_addchat(self, initial: str | None = None) -> None:
        if not self.chat_manager:
            await self._send_owner("Мастер добавления источников пока недоступен.")
            return
        self._reset_addchat()
        self._add_stage = "link"
        if initial:
            await self._consume_addchat(initial)
        else:
            await self._send_owner(
                "Пришли ссылку на Telegram-чат: https://t.me/name или https://t.me/+invite\n"
                "/cancel — отменить."
            )

    async def _consume_addchat(self, text: str) -> None:
        if text == "/cancel":
            self._reset_addchat()
            await self._send_owner("Добавление отменено.")
            return

        if self._add_stage == "link":
            try:
                self._candidate = await self.chat_manager.prepare(text)
            except Exception as e:
                await self._send_owner(f"Не получилось: {e}")
                return
            self._add_stage = "source_type"
            await self._send_owner(
                f"Нашёл: {self._candidate.chat.name}\n"
                "Выбери тип одним сообщением:\n" + ", ".join(self.chat_manager.source_types)
            )
            return

        if self._add_stage == "source_type":
            value = text.strip().lower()
            if value not in self.chat_manager.source_types:
                await self._send_owner("Неизвестный тип. Варианты:\n" +
                                       ", ".join(self.chat_manager.source_types))
                return
            self._source_type = value
            self._add_stage = "location"
            await self._send_owner("Укажи географию (например Ростов-на-Дону) или /skip.")
            return

        if self._add_stage == "location":
            self._location = None if text.strip() == "/skip" else text.strip()
            self._add_stage = "geo_gate"
            await self._send_owner(
                "Требовать явное упоминание Ростовской географии в сообщении?\n"
                "Ответь yes для федерального чата или no для локального."
            )
            return

        if self._add_stage == "geo_gate":
            require_location = text.strip().lower() in {"yes", "y", "да", "д"}
            try:
                chat = await self.chat_manager.activate(
                    self._candidate, self._source_type, self._location, require_location
                )
            except Exception as e:
                await self._send_owner(f"Ошибка активации: {e}")
                return
            self._reset_addchat()
            await self._send_owner(
                f"✅ Источник добавлен и уже мониторится.\n{chat.name}\n"
                f"type={chat.source_type}\nlocation={chat.location or '—'}\n"
                f"require_location={chat.require_location}"
            )

    async def _handle_update(self, upd: dict) -> None:
        cb = upd.get("callback_query")
        if cb:
            await self._handle_callback(cb)
            return
        msg = upd.get("message")
        if not msg or msg.get("chat", {}).get("id") != self.owner:
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return

        if text.startswith("/stats30"):
            await self._send_stats(30)
        elif text.startswith("/stats7"):
            await self._send_stats(7)
        elif text.startswith("/stats"):
            await self._send_stats(None)
        elif text.startswith("/addchat"):
            _, _, initial = text.partition(" ")
            await self._start_addchat(initial.strip() or None)
        elif self._add_stage:
            await self._consume_addchat(text)

    async def _handle_callback(self, cb: dict) -> None:
        cb_id = cb["id"]
        if cb.get("from", {}).get("id") != self.owner:
            await self.api("answerCallbackQuery", callback_query_id=cb_id, text="Нет доступа")
            return
        action, _, lead_id = (cb.get("data") or "").partition(":")
        if action not in FEEDBACK or not lead_id.isdigit():
            await self.api("answerCallbackQuery", callback_query_id=cb_id)
            return
        status, label = FEEDBACK[action]
        ok = self.db.set_feedback(int(lead_id), status)
        self.metrics.inc("true_positive" if status == "confirmed" else "false_positive")
        await self.api("answerCallbackQuery", callback_query_id=cb_id,
                       text=label if ok else "Лид не найден")
