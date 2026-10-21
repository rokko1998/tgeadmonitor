"""Calendar-month report formatting; no model or Telegram calls."""
from __future__ import annotations

from datetime import datetime

from src.time_utils import MSK, month_bounds

MONTH_NAMES = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


def format_monthly_report(stats: dict, *, through: datetime | None = None) -> dict:
    start, _ = month_bounds(stats["period"])
    title = f"📊 Ежемесячный отчёт — {MONTH_NAMES[start.month - 1]} {start.year}"
    if through is not None:
        title += f"\nПо состоянию на {through.astimezone(MSK):%d.%m %H:%M} МСК"
    def number(value: int) -> str:
        return f"{value:,}".replace(",", " ")
    lines = [
        title, "",
        f"Обработано сообщений: {number(stats['processed_messages'])}",
        f"Из чатов: {number(stats['processed_chats'])}",
        f"Сохранено всего: {number(stats['messages'])} из {number(stats['chats'])} чатов",
        f"Только архив, без оценки: {number(stats['archived_messages'])}", "",
        f"🔥 Найдено прямых лидов: {number(stats['direct_leads'])}",
        f"🟡 Найдено непрямых лидов: {number(stats['indirect_leads'])}",
        "Лиды посчитаны без повторных карточек, по времени обнаружения.",
    ]
    if stats["rejected_direct"] or stats["rejected_indirect"]:
        lines.append(f"Из них отмечено мусором: прямых {number(stats['rejected_direct'])}, "
                     f"непрямых {number(stats['rejected_indirect'])}")
    lines.extend(["", f"💳 Потрачено на Jev: ${stats['spent_nano'] / 1_000_000_000:.4f}"])
    if stats["reserved_nano"]:
        lines.append(f"Дополнительно зарезервировано, расход не подтверждён: "
                     f"${stats['reserved_nano'] / 1_000_000_000:.4f}")
    lines.append(f"Обращений к API Jev: {number(stats['api_attempts'])}")
    lines.append("Обработка включает бесплатный фильтр; архив без оценки указан отдельно.")
    lines.append("Расходы — по журналу этого мониторинга. Отчёт не вызывает Jev.")
    return {"text": "\n".join(lines)}
