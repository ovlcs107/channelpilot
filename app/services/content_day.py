from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True, slots=True)
class ContentSlot:
    time: str
    kind: str
    topic: str
    purpose: str

PRESET_DAY_PLANS: dict[str, list[ContentSlot]] = {
    "classic_news": [
        ContentSlot("10:00", "news", "главная новость дня", "быстро дать аудитории важный инфоповод"),
        ContentSlot("14:00", "digest", "короткий дайджест по теме канала", "закрыть несколько мелких инфоповодов"),
        ContentSlot("18:00", "prompt", "что эта новость значит для аудитории", "добавить редакционный контекст"),
        ContentSlot("21:00", "prompt", "вопрос аудитории по главной теме дня", "получить реакции и комментарии"),
    ],
    "meme_news": [
        ContentSlot("11:00", "news", "самый обсуждаемый инфоповод", "поймать актуальный шум"),
        ContentSlot("15:00", "prompt", "живая реакция на тренд", "сделать канал человечнее"),
        ContentSlot("19:00", "digest", "3 коротких новости дня", "дать плотный вечерний выпуск"),
        ContentSlot("22:00", "prompt", "ироничный вопрос аудитории", "собрать реакции"),
    ],
    "business": [
        ContentSlot("09:30", "news", "важная новость рынка", "дать деловой контекст"),
        ContentSlot("13:00", "prompt", "практический вывод для бизнеса", "показать пользу"),
        ContentSlot("17:30", "digest", "рынок и экономика", "собрать несколько событий"),
    ],
    "tech_ai": [
        ContentSlot("10:30", "news", "новость AI/Tech", "показать технологический инфоповод"),
        ContentSlot("16:00", "prompt", "как применить инструмент на практике", "дать пользу"),
        ContentSlot("20:00", "digest", "AI и технологии", "закрыть день дайджестом"),
    ],
}


def normalize_plan_preset(value: str | None) -> str:
    value = (value or "classic_news").strip().lower().replace("-", "_")
    aliases = {"news": "classic_news", "classic": "classic_news", "meme": "meme_news", "live": "meme_news", "tech": "tech_ai", "ai": "tech_ai", "expert": "expert_blog"}
    return aliases.get(value, value)


def build_day_plan(channel_username: str, preset: str | None = None, date_label: str = "завтра") -> str:
    key = normalize_plan_preset(preset)
    slots = PRESET_DAY_PLANS.get(key) or PRESET_DAY_PLANS["classic_news"]
    lines = [f"Контент-план для {channel_username} на {date_label}:", ""]
    for slot in slots:
        label = {"news": "новость", "digest": "дайджест", "prompt": "редакционный пост"}.get(slot.kind, slot.kind)
        lines.append(f"• {slot.time} — {label}: {slot.topic}")
        lines.append(f"  Цель: {slot.purpose}")
    lines.append("")
    lines.append("Публикация не выполняется автоматически. Сначала создаются черновики и проходят подтверждение.")
    return "\n".join(lines)
