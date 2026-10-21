"""Telegram lead/project-signal formatting."""
from __future__ import annotations
import html
import re
from src.storage.models import LeadRecord

MAX_TEXT = 700

def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)

def build_notification(lead: LeadRecord, chat_name: str) -> dict:
    m = lead.message
    text = m.text if len(m.text) <= MAX_TEXT else m.text[:MAX_TEXT] + "…"
    author = _esc(m.first_name) or "—"
    if m.user_id:
        author = f'<a href="tg://user?id={m.user_id}">{author}</a>'
    username = f"@{m.username}" if m.username else ""

    indirect=lead.lead_type in ("indirect","project_signal")
    title="🟡 <b>Непрямой лид</b>" if indirect else "🔥 <b>Прямой лид</b>"
    lines = [
        title, "",
        f"📦 {_esc(lead.product) or '— (косвенная потребность)'}",
        f"📍 {_esc(lead.location) or '—'}",
        ("Контакт для воронки: покупка наших материалов не заявлена."
         if indirect else "Запрос на наши материалы или изготовление из металла."), "",
        "💬 <b>Сообщение:</b>",
        f"<i>{_esc(text)}</i>", "",
        f"👤 {author} {_esc(username)}".rstrip(), "",
        f"🏘 Чат: {_esc(chat_name)}",
    ]

    row1 = []
    if m.message_url:
        row1.append({"text": "Открыть сообщение", "url": m.message_url})
    if m.username:
        row1.append({"text": "Написать автору", "url": f"https://t.me/{m.username}"})
    row2 = [
        {"text": "✅ Полезно", "callback_data": f"lead:{lead.lead_id}"},
        {"text": "❌ Мусор", "callback_data": f"notlead:{lead.lead_id}"},
    ]
    return {"text": "\n".join(lines),
            "reply_markup": {"inline_keyboard": [r for r in (row1, row2) if r]}}


def build_indirect_digest(rows, period: str) -> list[dict]:
    """No extra model calls: summaries are safe excerpts from stored originals."""
    heading=f"🟡 <b>Сводка непрямых лидов</b>\n{_esc(period)}\nВсего без повторов: {len(rows)}\n"
    if not rows:return [{"text":heading+"\nНовых непрямых возможностей нет.","parse_mode":"HTML"}]
    pages=[];text=heading;buttons=[]
    for row in rows:
        excerpt=re.sub(r"\s+"," ",row["original_text"]).strip()[:160]
        label=f"#{row['id']}"
        if row["message_url"]:
            label=f'<a href="{html.escape(row["message_url"],quote=True)}">{label}</a>'
        line=f"\n{label} · {_esc(row['chat_name'])}\n{_esc(excerpt)}\n"
        if len(text)+len(line)>3500 and buttons:
            pages.append({"text":text,"parse_mode":"HTML","reply_markup":{"inline_keyboard":buttons}})
            text=heading;buttons=[]
        text+=line
        buttons.append([{"text":f"✅ #{row['id']}","callback_data":f"lead:{row['id']}"},
                        {"text":f"❌ #{row['id']}","callback_data":f"notlead:{row['id']}"}])
    pages.append({"text":text,"parse_mode":"HTML","reply_markup":{"inline_keyboard":buttons}})
    return pages
