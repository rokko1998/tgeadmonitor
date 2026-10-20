"""Bot API notifications, feedback, analytics and batch source onboarding."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from src.metrics import Metrics
from src.storage.database import Database
from src.telegram.chat_links import extract_links, parse_link

log = logging.getLogger("notifier")
FEEDBACK = {"lead": ("confirmed", "✅ Отмечено как лид"),
            "notlead": ("rejected", "❌ Отмечено: не лид")}
HELP = (
    "Пришли одну или несколько ссылок на Telegram-чаты одним сообщением.\n"
    "Можно без команды или так: /addchat https://t.me/name https://t.me/+invite\n"
    "Поддерживаются @username и ссылки-приглашения. Мониторинг включится без перезапуска.\n"
    "Закрытый чат с одобрением появится после принятия заявки.\n\n"
    "/chats — активные источники и состояние очереди\n"
    "/stats, /stats7, /stats30 — статистика"
)


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
        except (aiohttp.ClientError, asyncio.TimeoutError, BotApiError) as exc:
            final = row["attempts"] + 1 >= self.max_attempts
            self.db.mark_notification_failed(int(row["id"]), str(exc), final=final)
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
                    # Acknowledge only after its source links have been persisted.
                    await self._handle_update(upd)
                    self._offset = upd["update_id"] + 1
            except (aiohttp.ClientError, asyncio.TimeoutError, BotApiError) as exc:
                log.warning("Polling error: %s", exc)
                await asyncio.sleep(self.retry_interval)
            except Exception:
                log.exception("Polling loop error")
                await asyncio.sleep(self.retry_interval)

    async def send_owner(self, text: str) -> None:
        # Plain text: titles, links and API errors never become HTML markup.
        for start in range(0, len(text), 3900):
            await self.api("sendMessage", chat_id=self.owner, text=text[start:start + 3900],
                           disable_web_page_preview=True)

    async def _send_stats(self, days: int | None) -> None:
        stats = self.db.stats(days)
        stats.update(self.metrics.snapshot())
        stats["source_requests"] = self.db.source_request_counts()
        await self.send_owner(json.dumps(stats, ensure_ascii=False, indent=1))

    async def _add_links(self, text: str) -> None:
        if not self.chat_manager:
            await self.send_owner("Добавление источников пока недоступно.")
            return
        links = extract_links(text)
        if not links:
            await self.send_owner(HELP)
            return
        seen, lines = set(), []
        for raw in links:
            try:
                link = parse_link(raw)
                if link.key in seen:
                    continue
                seen.add(link.key)
                status = self.chat_manager.submit(raw)
                lines.append(f"{link.url} — {status}")
            except ValueError as exc:
                lines.append(f"{raw} — ❌ {exc}")
        await self.send_owner("Результат приёма ссылок:\n" + "\n".join(lines))

    async def _handle_update(self, upd: dict) -> None:
        cb = upd.get("callback_query")
        if cb:
            await self._handle_callback(cb)
            return
        msg = upd.get("message")
        if not msg or msg.get("chat", {}).get("id") != self.owner:
            return
        # Management is private to OWNER_CHAT_ID; forwarded messages cannot grant access.
        if msg.get("from", {}).get("id") != self.owner:
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        links_text = text + "\n" + "\n".join(
            e["url"] for e in [*(msg.get("entities") or []), *(msg.get("caption_entities") or [])]
            if e.get("type") == "text_link" and e.get("url")
        )
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        if command in {"/start", "/help"}:
            await self.send_owner(HELP)
        elif command in {"/stats", "/stats7", "/stats30"}:
            await self._send_stats({"/stats": None, "/stats7": 7, "/stats30": 30}[command])
        elif command == "/chats" and self.chat_manager:
            chats = self.chat_manager.listener.resolved.values()
            lines = [f"Активных источников: {len(self.chat_manager.listener.resolved)}"]
            lines.extend(f"• {c.name} — @{c.username}" if c.username else f"• {c.name} ({c.chat_id})" for c in chats)
            lines.append("Очередь: " + json.dumps(self.db.source_request_counts(), ensure_ascii=False))
            await self.send_owner("\n".join(lines))
        elif command == "/addchat" or extract_links(links_text):
            await self._add_links(links_text)

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
        if ok:
            self.metrics.inc("true_positive" if status == "confirmed" else "false_positive")
        await self.api("answerCallbackQuery", callback_query_id=cb_id,
                       text=label if ok else "Лид не найден")
