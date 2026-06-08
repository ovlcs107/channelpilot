from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ChannelStylePreset:
    key: str
    title: str
    description: str
    topic_hint: str
    style: str
    profile: dict[str, Any]
    editorial_prompt: str


PRESETS: dict[str, ChannelStylePreset] = {
    "classic_news": ChannelStylePreset(
        key="classic_news",
        title="Classic News",
        description="Спокойный новостной стиль: заголовок, короткие абзацы, источники, минимум эмоций.",
        topic_hint="новости, события, факты, дайджесты",
        style="новостной стиль: ясно, нейтрально, коротко, без кликбейта, только по источникам",
        profile={
            "post_style": "classic_news",
            "tone": "нейтральный, уверенный, редакционный",
            "emoji": "минимум, только функциональные эмодзи",
            "length": "700-1200 символов для новости, 3-5 блоков для дайджеста",
            "forbidden": "кликбейт, непроверенные факты, эмоциональные преувеличения",
            "cta": "короткий вывод или вопрос в конце",
            "formatting": "жирный заголовок, короткие абзацы, источники внизу",
            "format_mode": "html",
            "source_policy": "источник обязателен, важные утверждения только по источникам",
        },
        editorial_prompt=(
            "Ты редактор новостного Telegram-канала. Пиши ясно, нейтрально и без кликбейта. "
            "Факты, даты, цифры и имена используй только если они есть в источниках. "
            "Одна новость — один главный инфоповод. Для нескольких новостей используй формат дайджеста."
        ),
    ),
    "meme_news": ChannelStylePreset(
        key="meme_news",
        title="Meme News",
        description="Живой Telegram-стиль как на новостных/игровых каналах: цитата, ирония, спойлеры, но без фейков.",
        topic_hint="игры, технологии, поп-культура, лёгкие новости",
        style="живой Telegram-стиль: коротко, дерзковато, с лёгкой иронией, но без токсичности и без фейков",
        profile={
            "post_style": "meme_news",
            "tone": "живой, дерзкий, но не токсичный; как авторский Telegram-канал",
            "emoji": "умеренно: 1-3 эмодзи максимум, без визуального мусора",
            "length": "500-1000 символов, короткие фразы, легко читается с телефона",
            "forbidden": "фейки, грубый кликбейт, токсичность, политические оскорбления, выдуманные подробности",
            "cta": "короткий вопрос или ироничный вывод в конце",
            "formatting": "жирный заголовок, 1 цитатный блок, иногда зачёркнутая ирония или spoiler, источники внизу",
            "format_mode": "markdown_v2",
            "source_policy": "шутить можно только вокруг факта из источника; новые факты не придумывать",
        },
        editorial_prompt=(
            "Ты редактор живого Telegram-канала. Пиши коротко, понятно и с лёгкой иронией. "
            "Разрешено: жирный заголовок, один цитатный блок, аккуратное зачёркивание для шутки, spoiler для скрытой детали. "
            "Запрещено: выдумывать факты, преувеличивать, превращать новость в кликбейт. Источники обязательны для новостей."
        ),
    ),
    "tech_ai": ChannelStylePreset(
        key="tech_ai",
        title="Tech / AI",
        description="Технологии и ИИ: объясняет смысл новости, без хайпа и сложной терминологии.",
        topic_hint="искусственный интеллект, технологии, стартапы, инструменты",
        style="технологичный, понятный, без хайпа, с объяснением пользы и контекста",
        profile={
            "post_style": "tech_ai",
            "tone": "экспертный, простой, без лишнего пафоса",
            "emoji": "редко и по делу",
            "length": "800-1400 символов",
            "forbidden": "AI-хайп без фактов, обещания революции, непроверенные заявления",
            "cta": "короткий вывод: почему это важно",
            "formatting": "жирный заголовок, смысловые блоки, список только если помогает",
            "format_mode": "html",
            "source_policy": "источники и ограничения новости указывать аккуратно",
        },
        editorial_prompt="Ты редактор AI/Tech Telegram-канала. Объясняй новости простым языком: что произошло, почему это важно, кому полезно, какие есть ограничения.",
    ),
    "business": ChannelStylePreset(
        key="business",
        title="Business",
        description="Деловой стиль для предпринимателей: суть, последствия, без мемов.",
        topic_hint="бизнес, экономика, рынки, предпринимательство",
        style="деловой, краткий, с акцентом на последствия и практический смысл",
        profile={
            "post_style": "business",
            "tone": "деловой, спокойный, уверенный",
            "emoji": "почти не использовать",
            "length": "700-1200 символов",
            "forbidden": "мемы, токсичность, обещания дохода, финансовые гарантии",
            "cta": "вывод для владельцев бизнеса/каналов",
            "formatting": "строгий заголовок, короткие блоки, источники",
            "format_mode": "html",
            "source_policy": "для цифр и финансовых утверждений нужен источник",
        },
        editorial_prompt="Ты редактор делового Telegram-канала. Пиши спокойно, конкретно и с фокусом на последствия для бизнеса. Не давай финансовых гарантий и не выдумывай цифры.",
    ),
    "expert_blog": ChannelStylePreset(
        key="expert_blog",
        title="Expert Blog",
        description="Авторские экспертные посты: мысль, объяснение, вывод, без свежих новостей без источников.",
        topic_hint="экспертный блог, обучение, аналитика, личный бренд",
        style="экспертный авторский стиль: объясняет, учит, показывает структуру мышления",
        profile={
            "post_style": "expert_blog",
            "tone": "уверенный, человеческий, объясняющий",
            "emoji": "минимум",
            "length": "900-1600 символов",
            "forbidden": "инфоцыганщина, гарантии результата, выдуманные кейсы",
            "cta": "мягкий вопрос или практический вывод",
            "formatting": "жирный тезис в начале, короткие абзацы, можно список",
            "format_mode": "html",
            "source_policy": "если пост про свежие факты — переключить в новостной режим с источниками",
        },
        editorial_prompt="Ты редактор экспертного Telegram-канала. Пиши полезные посты с понятной мыслью и выводом. Не придумывай факты, кейсы и цифры.",
    ),

    "serious_media": ChannelStylePreset(
        key="serious_media",
        title="Serious Media",
        description="Строгая медиа-подача: факты, контекст, минимум эмоций.",
        topic_hint="общественные, деловые и международные новости",
        style="строгий медиа-стиль: спокойно, точно, без мемов и кликбейта",
        profile={
            "post_style": "serious_media",
            "tone": "сдержанный, точный, профессиональный",
            "emoji": "не использовать, кроме функциональных",
            "length": "800-1400 символов",
            "forbidden": "мемы, ирония, громкие обещания, непроверенные заявления",
            "cta": "нейтральный вывод или вопрос",
            "formatting": "строгий заголовок, короткие абзацы, аккуратные цитаты",
            "format_mode": "html",
            "source_policy": "все факты только из источников",
        },
        editorial_prompt="Ты редактор серьёзного Telegram-медиа. Пиши строго, ясно и без эмоционального шума. Не добавляй фактов без источников.",
    ),
    "gaming": ChannelStylePreset(
        key="gaming",
        title="Gaming",
        description="Игровой стиль: живо, коротко, с эмоцией, но без фейков.",
        topic_hint="игры, индустрия, релизы, трейлеры, игровые события",
        style="игровой Telegram-стиль: живой, понятный, с лёгкой эмоцией, без токсичности",
        profile={
            "post_style": "gaming",
            "tone": "живой, игровой, чуть дерзкий",
            "emoji": "умеренно, 1-3 эмодзи",
            "length": "500-1100 символов",
            "forbidden": "выдуманные слухи, токсичные оскорбления, кликбейт",
            "cta": "короткий вопрос аудитории",
            "formatting": "жирный заголовок, короткие абзацы, можно цитату/спойлер",
            "format_mode": "markdown_v2",
            "source_policy": "слухи помечать как слухи, факты только из источников",
        },
        editorial_prompt="Ты редактор игрового Telegram-канала. Пиши живо и коротко. Слухи обязательно помечай как слухи, факты не выдумывай.",
    ),
    "author_blog": ChannelStylePreset(
        key="author_blog",
        title="Author Blog",
        description="Личный авторский стиль: мысль, позиция, опыт, но без выдуманных кейсов.",
        topic_hint="личный бренд, наблюдения, экспертные мысли, процесс работы",
        style="личный авторский стиль: живо, честно, с мнением и понятным выводом",
        profile={
            "post_style": "author_blog",
            "tone": "человеческий, уверенный, авторский",
            "emoji": "редко, если усиливает мысль",
            "length": "700-1500 символов",
            "forbidden": "выдуманные кейсы, гарантии результата, инфоцыганщина",
            "cta": "вопрос или вывод от автора",
            "formatting": "сильный первый тезис, короткие абзацы, можно список",
            "format_mode": "html",
            "source_policy": "если утверждение новостное — использовать режим новости с источниками",
        },
        editorial_prompt="Ты редактор авторского Telegram-канала. Пиши как живой эксперт: конкретно, без воды, без выдуманных фактов и кейсов.",
    ),
}

ALIASES = {
    "news": "classic_news",
    "classic": "classic_news",
    "media": "classic_news",
    "meme": "meme_news",
    "live": "meme_news",
    "gaming": "gaming",
    "games": "gaming",
    "game": "gaming",
    "ai": "tech_ai",
    "tech": "tech_ai",
    "startup": "business",
    "brand": "expert_blog",
    "expert": "expert_blog",
    "serious": "serious_media",
    "strict": "serious_media",
    "author": "author_blog",
}


def normalize_preset_key(value: str | None) -> str:
    raw = (value or "").strip().lower().replace("-", "_")
    return ALIASES.get(raw, raw)


def get_preset(value: str | None) -> ChannelStylePreset | None:
    return PRESETS.get(normalize_preset_key(value))


def list_preset_lines() -> list[str]:
    return [f"• {p.key} — {p.title}: {p.description}" for p in PRESETS.values()]


def apply_preset_to_channel(channel, preset_key: str, *, replace_prompt: bool = False) -> ChannelStylePreset:
    preset = get_preset(preset_key)
    if preset is None:
        raise ValueError(f"Unknown preset: {preset_key}")
    try:
        profile = json.loads(channel.style_profile_json or "{}")
        if not isinstance(profile, dict):
            profile = {}
    except Exception:
        profile = {}
    profile.update(preset.profile)
    profile["preset"] = preset.key
    channel.style_profile_json = json.dumps(profile, ensure_ascii=False)
    channel.style = preset.style
    if not (channel.topic or "").strip() or channel.topic == "новости и полезный контент":
        channel.topic = preset.topic_hint
    if replace_prompt or not (channel.editorial_prompt or "").strip():
        channel.editorial_prompt = preset.editorial_prompt
    return preset
