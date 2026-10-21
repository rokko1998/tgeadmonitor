"""Bot API notifications, feedback, analytics and batch source onboarding."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from src.metrics import Metrics
from src.storage.database import Database
from src.telegram.chat_links import extract_links, parse_link
from src.telegram.formatter import build_indirect_digest
from src.reporting import format_monthly_report
from src.time_utils import MSK, month_bounds
from src.storage.database import _utc

log = logging.getLogger("notifier")
FEEDBACK = {"lead": ("confirmed", "✅ Сохранено: полезный контакт"),
            "notlead": ("rejected", "❌ Сохранено: мусор")}
HELP = (
    "Я ищу покупателей металлопроката, пиломатериалов и изготовления из металла "
    "в подключённых Telegram-чатах. Регионы не ограничены.\n\n"
    "КАК ПОДКЛЮЧИТЬ ЧАТ\n"
    "Пришли @имя_чата или ссылку. Можно сразу несколько, каждую с новой строки:\n"
    "@example_chat\nhttps://t.me/another_chat\nhttps://t.me/+приглашение\n"
    "Команда необязательна; можно /addchat @example_chat. "
    "Я сообщу результат по каждому адресу. Если нужно одобрение администратора, "
    "проверю вступление каждые 5 минут и включу мониторинг автоматически. "
    "Задача сохранится после перезапуска. Личные аккаунты и боты не подходят.\n\n"
    "КАК ЧИТАТЬ КАРТОЧКИ\n"
    "🔥 Прямой лид — запрос на наши товары или изготовление/обработку металла.\n"
    "🟡 Непрямой лид — полезный строительный контакт для дальнейшего общения; "
    "покупка наших материалов не заявлена. Такие карточки приходят сразу, "
    "а в 21:00 МСК — сводка без повторов.\n"
    "Короткие запросы без деталей и сомнительные случаи сохраняю без рассылки. "
    "Явную рекламу отсекаю до платной проверки Jev.\n"
    "В карточке можно открыть исходное сообщение и написать автору, если у него есть @имя. "
    "Нажми «✅ Полезно» или «❌ Мусор»: оценка сохранится для анализа качества. "
    "Отмеченное мусором не попадёт в следующую сводку. Оценку можно изменить повторным нажатием.\n\n"
    "КОМАНДЫ\n"
    "/help — эта инструкция.\n"
    "/chats — список чатов, их номера, режим и качество за период наблюдения.\n"
    "/pause ID — отключить Jev и карточки для чата; новые сообщения останутся в архиве.\n"
    "/resume ID — вернуть проверку новых сообщений. Архив за паузу автоматически не рассылается.\n"
    "/keep ID — защитить важный чат от автоматической паузы.\n"
    "/unkeep ID — убрать защиту.\n"
    "ID — число в скобках из /chats, включая минус. Пример: /pause -1001234567890.\n"
    "Автопауза: по умолчанию после 30 дней наблюдения и 1000 уникальных сообщений "
    "без полезных возможностей; я пришлю причину. Архив продолжит пополняться.\n"
    "/indirect или /digest — непрямые лиды за последние 24 часа.\n"
    "/jev — расходы, месячный лимит и очередь проверки. "
    "Если лимит исчерпан или API недоступен, сообщения ждут в БД, рассылка без проверки не включается.\n"
    "/jevretry — повторить проверки после устранения ошибки API; месячный лимит сохраняется.\n"
    "/stats — статистика за всё время; /stats7 — за неделю; /stats30 — за последние 30 дней.\n\n"
    "1-го числа в 09:00 МСК — отчёт за предыдущий календарный месяц: сообщения, чаты, "
    "прямые и непрямые лиды, расходы Jev.\n"
    "/month — отчёт за текущий месяц на данный момент; /month 2026-10 — за указанный месяц.\n\n"
    "Управление доступно владельцу в личном диалоге либо администраторам настроенной группы. "
    "Пересылка примера боту сама по себе не запускает проверку лида."
)


class BotApiError(Exception):
    pass


class Notifier:
    def __init__(self, token: str, owner_chat_id: int, db: Database, metrics: Metrics,
                 retry_interval: float = 5.0, max_attempts: int = 50,
                 monthly_budget_usd: float = 4.0):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.owner, self.db, self.metrics = owner_chat_id, db, metrics
        self.retry_interval, self.max_attempts = retry_interval, max_attempts
        self.monthly_budget_usd = monthly_budget_usd
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
        try:
            await self.api("setMyCommands", commands=[
                {"command": name, "description": description} for name, description in (
                    ("help", "Как пользоваться ботом"), ("addchat", "Добавить чаты ссылками или @именами"),
                    ("chats", "Чаты, режимы и качество"), ("indirect", "Непрямые лиды за сутки"),
                    ("jev", "Расходы и очередь проверки"), ("pause", "Пауза оценки: /pause ID"),
                    ("resume", "Вернуть оценку: /resume ID"), ("keep", "Защитить от автопаузы: /keep ID"),
                    ("unkeep", "Убрать защиту: /unkeep ID"), ("jevretry", "Повторить проверки с ошибкой"),
                    ("stats", "Статистика за всё время"), ("stats7", "Статистика за неделю"),
                    ("stats30", "Статистика за месяц"),
                    ("month", "Месячный отчёт: /month или /month 2026-10"),
                )])
        except BotApiError:
            log.warning("Could not update bot command menu")

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
                for row in self.db.pending_system_notifications():
                    await self._send_system(row)
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

    async def _send_system(self, row: Any) -> None:
        payload=json.loads(row["payload"])
        try:
            await self.api("sendMessage",chat_id=self.owner,text=payload["text"],
                parse_mode=payload.get("parse_mode"),disable_web_page_preview=True,
                reply_markup=payload.get("reply_markup"))
        except (aiohttp.ClientError,asyncio.TimeoutError,BotApiError) as exc:
            self.db.finish_system_notification(row["id"],type(exc).__name__)
            return
        self.db.finish_system_notification(row["id"])

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
        stats["jev"] = self.db.semantic_stats()
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
        if not msg or not await self._authorized(msg.get("from", {}), msg.get("chat", {}).get("id")):
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
        elif command == "/month":
            now = datetime.now(MSK)
            parts = text.split()
            period = parts[1] if len(parts) == 2 else now.strftime("%Y-%m")
            try:
                start, end = month_bounds(period)
                if len(parts) > 2 or start > now:
                    raise ValueError("Invalid report period")
            except ValueError:
                await self.send_owner("Формат: /month или /month ГГГГ-ММ, например /month 2026-10. Месяц не должен быть будущим.")
                return
            snapshot = self.db.conn.execute("SELECT payload FROM system_notifications WHERE key=?",
                                            (f"monthly-report:{period}",)).fetchone()
            if snapshot:
                payload = json.loads(snapshot["payload"])
            else:
                through = now if now < end else None
                payload = format_monthly_report(self.db.monthly_report_stats(period,through=through),through=through)
            await self.send_owner(payload["text"])
        elif command == "/chats" and self.chat_manager:
            chats=self.db.source_health()
            resolved=self.chat_manager.listener.resolved
            pending=self.db.conn.execute("SELECT COUNT(*) FROM source_seeds WHERE resolved_chat_id IS NULL").fetchone()[0]
            lines=[f"Источников в БД: {len(chats)}. Подключено сейчас: {len(resolved)}. Ожидает доступа/разрешения имени: {pending}."]
            for c in chats:
                status=("⛔ Отключён" if not c["enabled"] else "⏳ Нет подключения" if c["chat_id"] not in resolved
                        else "✅ Jev" if c["analysis_enabled"] else "⏸ Архив без Jev")
                lines.append(f"{status} · {c['name']} ({c['chat_id']}) · полезных: {c['useful']} / уникальных: {c['unique_messages']}"+(" · 🔒" if c["pinned"] else ""))
            if pending:
                for row in self.db.conn.execute("SELECT chat_json FROM source_seeds WHERE resolved_chat_id IS NULL"):
                    seed=json.loads(row['chat_json'])
                    if seed.get('enabled',True):
                        lines.append(f"⏳ Ещё не подключён · {seed['name']} · @{seed.get('username','—')}")
            lines.append("Очередь: " + json.dumps(self.db.source_request_counts(), ensure_ascii=False))
            await self.send_owner("\n".join(lines))
        elif command in {"/pause","/resume","/keep","/unkeep"}:
            parts=text.split()
            try:
                chat_id=int(parts[1]) if len(parts)==2 else None
            except ValueError:
                chat_id=None
            if chat_id is None:
                await self.send_owner(f"Формат: {command} ID. Номера есть в /chats.")
                return
            ok=False
            if command in {"/keep","/unkeep"}:
                ok=self.db.set_source_pinned(chat_id,command=="/keep")
            elif command=="/pause":
                ok=self.db.set_source_analysis(chat_id,False)
            elif self.chat_manager:
                try:
                    ok=await self.chat_manager.resume(chat_id)
                except (ValueError, OSError, asyncio.TimeoutError):
                    await self.send_owner("Не удалось подключиться к чату. Проверь доступ аккаунта и повтори /resume позже.")
                    return
            await self.send_owner("✅ Сохранено." if ok else "Источник не найден или пока недоступен.")
        elif command=="/jev":
            stats=self.db.semantic_stats()
            await self.send_owner(f"Jev · {stats['month']}\nПотрачено/зарезервировано: ${stats['spent_or_reserved_usd']:.4f} из ${self.monthly_budget_usd:g} в месяц\nОчередь: "+json.dumps(stats["jobs"],ensure_ascii=False))
        elif command=="/jevretry":
            with self.db.conn:
                count=self.db.conn.execute("UPDATE semantic_jobs SET state='queued',next_attempt=0,attempts=0 WHERE state='failed'").rowcount
            await self.send_owner(f"Повторно поставлено на проверку: {count}. Лимит расходов сохраняется.")
        elif command in {"/indirect","/digest"}:
            now=datetime.now(timezone.utc)
            rows=self.db.indirect_since(_utc(now-timedelta(days=1)),_utc(now))
            for i,payload in enumerate(build_indirect_digest(rows,"Последние 24 часа")):
                self.db.enqueue_system(f"manual-digest:{upd['update_id']}:{i}",payload)
            self.wakeup()
        elif command == "/addchat" or extract_links(links_text):
            await self._add_links(links_text)
        elif text.startswith("/"):
            await self.send_owner("Команда не найдена. Открой /help — там инструкция и примеры.")

    async def _authorized(self, sender: dict, chat_id: int | None) -> bool:
        if chat_id != self.owner or not sender.get("id") or sender.get("is_bot"):
            return False
        if self.owner > 0:
            return sender["id"] == self.owner
        # A group administrator may manage the configured operator group.
        try:
            member = await self.api("getChatMember", chat_id=self.owner, user_id=sender["id"])
        except (BotApiError, aiohttp.ClientError, asyncio.TimeoutError):
            return False
        return member.get("status") in {"creator", "administrator"}

    async def _handle_callback(self, cb: dict) -> None:
        cb_id = cb["id"]
        if not await self._authorized(cb.get("from", {}), cb.get("message", {}).get("chat", {}).get("id", self.owner)):
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
