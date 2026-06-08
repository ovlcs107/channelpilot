from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.models import Channel
from app.services.ai_writer import AIWriter
from app.utils.text import truncate

_ALLOWED_ACTIONS = {
    "connect_channel",
    "check_channel",
    "set_prompt",
    "show_prompt",
    "create_news",
    "create_digest",
    "create_prompt_post",
    "show_queue",
    "show_calendar",
    "show_channels",
    "show_usage",
    "show_limits",
    "show_audit",
    "content_day",
    "export_channel",
    "set_format",
    "set_preset",
    "add_source",
    "add_source_pack",
    "help",
    "unknown",
}

_DANGEROUS_WORDS = {
    "ignore previous",
    "ignore all",
    "забудь правила",
    "игнорируй правила",
    "обойди защиту",
    "без проверки",
    "без подтверждения",
    "любой канал",
    "чужой канал",
}


@dataclass(slots=True)
class RouteResult:
    action: str = "unknown"
    channels: list[str] = field(default_factory=list)
    topic: str = ""
    prompt_text: str = ""
    source_url: str = ""
    source_pack: str = ""
    format_mode: str = ""
    preset: str = ""
    confidence: float = 0.0
    clarification: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_channel(self) -> bool:
        return self.action in {
            "connect_channel",
            "check_channel",
            "set_prompt",
            "show_prompt",
            "create_news",
            "create_digest",
            "create_prompt_post",
            "show_queue",
            "show_calendar",
            "show_usage",
            "show_limits",
            "show_audit",
            "content_day",
            "export_channel",
            "set_format",
            "set_preset",
            "add_source",
            "add_source_pack",
        }

    @property
    def is_write_action(self) -> bool:
        return self.action in {
            "connect_channel",
            "set_prompt",
            "create_news",
            "create_digest",
            "create_prompt_post",
            "set_format",
            "set_preset",
            "add_source",
            "add_source_pack",
        }


def _norm_channel(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    if not value.startswith("@"):
        value = "@" + value
    return value


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class NaturalLanguageRouter:
    """Turns normal Russian/English messages into safe internal actions.

    The router never publishes, never writes to DB directly, and never bypasses
    channel ownership checks. It only returns an intent; handlers execute it
    through the same safety guards as slash commands.
    """

    def __init__(self, writer: AIWriter, *, use_ai: bool = True) -> None:
        self.writer = writer
        self.use_ai = use_ai

    async def route(self, text: str, channels: list[Channel], owner_telegram_id: int = 0) -> RouteResult:
        text = (text or "").strip()
        if not text:
            return RouteResult(action="unknown", clarification="Напиши задачу текстом: что сделать и для какого канала.")
        lowered = text.lower()
        if any(word in lowered for word in _DANGEROUS_WORDS):
            return RouteResult(action="unknown", confidence=0.1, clarification="Не могу выполнить задачу, потому что она пытается обойти проверки безопасности или публикацию без подтверждения.")

        heuristic = self._heuristic_route(text, channels)
        if heuristic.confidence >= 0.82 or not self.use_ai:
            return heuristic

        ai = await self._ai_route(text, channels, owner_telegram_id)
        if ai.action != "unknown" and ai.confidence >= 0.45:
            return ai
        if heuristic.action != "unknown":
            return heuristic
        return ai

    async def _ai_route(self, text: str, channels: list[Channel], owner_telegram_id: int) -> RouteResult:
        available = [
            {
                "username": ch.username,
                "title": ch.title or ch.username,
                "topic": truncate(ch.topic, 180),
                "verified": bool(ch.is_verified),
                "has_prompt": bool((ch.editorial_prompt or "").strip()),
            }
            for ch in channels
        ]
        prompt = [
            {
                "role": "system",
                "content": (
                    "Ты безопасный intent-router для Telegram AI-SMM бота. "
                    "Ты НЕ выполняешь действия, а только выбираешь одно действие из allowlist и возвращаешь строгий JSON. "
                    "Нельзя добавлять каналы, которых нет в available_channels, кроме connect_channel/check_channel если пользователь явно указал @username. "
                    "Если пользователь просит обойти проверки, публиковать без подтверждения, работать с чужим каналом или задача неясна — action=unknown. "
                    "Верни только JSON без markdown."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "allowed_actions": sorted(_ALLOWED_ACTIONS),
                        "available_channels": available,
                        "user_message": text,
                        "schema": {
                            "action": "one of allowed_actions",
                            "channels": ["@channel"],
                            "topic": "topic/task for post/news/digest",
                            "prompt_text": "editorial prompt if user sets it",
                            "source_url": "rss url if user adds source",
                            "source_pack": "pack key like ru_world/world_general/games_global",
                            "format_mode": "html|markdown_v2|plain if user asks format",
                            "preset": "classic_news|meme_news|tech_ai|business|expert_blog if user asks style preset",
                            "confidence": 0.0,
                            "clarification": "question if needed",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = await self.writer._chat(prompt, purpose="router", owner_telegram_id=owner_telegram_id)  # noqa: SLF001
        data = _extract_json(response or "")
        if not data:
            return RouteResult(action="unknown", confidence=0.0, clarification="Не удалось точно определить задачу. Укажите канал и нужное действие.")
        return self._from_dict(data, channels, text)

    def _from_dict(self, data: dict[str, Any], channels: list[Channel], text: str) -> RouteResult:
        action = str(data.get("action") or "unknown").strip().lower()
        if action not in _ALLOWED_ACTIONS:
            action = "unknown"
        channels_in = data.get("channels") or []
        if isinstance(channels_in, str):
            channels_in = [channels_in]
        channels_norm = self._sanitize_channels([str(item) for item in channels_in], channels, text, allow_explicit=True)
        confidence_raw = data.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except Exception:
            confidence = 0.0
        return RouteResult(
            action=action,
            channels=channels_norm,
            topic=str(data.get("topic") or "").strip(),
            prompt_text=str(data.get("prompt_text") or "").strip(),
            source_url=str(data.get("source_url") or "").strip(),
            source_pack=str(data.get("source_pack") or "").strip().lower(),
            format_mode=str(data.get("format_mode") or "").strip().lower(),
            preset=str(data.get("preset") or "").strip().lower(),
            confidence=max(0.0, min(1.0, confidence)),
            clarification=str(data.get("clarification") or "").strip(),
            raw=data,
        )

    def _heuristic_route(self, text: str, channels: list[Channel]) -> RouteResult:
        lowered = text.lower()
        explicit_channels = re.findall(r"@[a-zA-Z0-9_]{4,64}", text)
        channels_norm = self._sanitize_channels(explicit_channels, channels, text, allow_explicit=True)

        def result(action: str, confidence: float, **kwargs: Any) -> RouteResult:
            return RouteResult(action=action, channels=channels_norm.copy(), confidence=confidence, **kwargs)

        source_url_match = re.search(r"https?://\S+", text)
        pack_match = re.search(r"\b(ru_general|ru_world|world_general|world_business|tech_global|games_global)\b", lowered)

        if any(w in lowered for w in ["помощ", "что ты умеешь", "как пользоваться", "команды"]):
            return result("help", 0.9)
        if any(w in lowered for w in ["мои каналы", "список канал", "какие каналы"]):
            return result("show_channels", 0.9)
        if "очеред" in lowered:
            return result("show_queue", 0.85)
        if "календар" in lowered:
            return result("show_calendar", 0.85)
        if any(w in lowered for w in ["расход", "usage", "потрачен", "генераций сегодня", "токен"]):
            return result("show_usage", 0.86)
        if "лимит" in lowered:
            return result("show_limits", 0.86)
        if any(w in lowered for w in ["audit", "аудит", "история действий", "журнал действий"]):
            return result("show_audit", 0.86)
        if any(w in lowered for w in ["день контента", "контент на завтра", "план на завтра", "подготовь день"]):
            return result("content_day", 0.84, topic=self._topic_without_channels(text))
        if any(w in lowered for w in ["экспорт", "backup", "бэкап", "выгрузи настройки"]):
            return result("export_channel", 0.84)
        if any(w in lowered for w in ["проверь", "проверить", "чекни"]) and "канал" in lowered:
            return result("check_channel", 0.86)
        if any(w in lowered for w in ["подключ", "добавь канал", "добавить канал", "привяж"]):
            return result("connect_channel", 0.82)
        if "промпт" in lowered or "редакционн" in lowered:
            if any(w in lowered for w in ["покажи", "посмотреть", "какой"]):
                return result("show_prompt", 0.82)
            # Text after ':' or '|' is likely the prompt.
            prompt_text = ""
            if "|" in text:
                prompt_text = text.split("|", 1)[1].strip()
            elif ":" in text:
                prompt_text = text.split(":", 1)[1].strip()
            return result("set_prompt", 0.78, prompt_text=prompt_text)
        if "markdown" in lowered or "маркдаун" in lowered:
            return result("set_format", 0.86, format_mode="markdown_v2")
        if "html" in lowered and "формат" in lowered:
            return result("set_format", 0.86, format_mode="html")
        if "plain" in lowered or "без формат" in lowered:
            return result("set_format", 0.82, format_mode="plain")
        preset_words = {
            "meme_news": ["meme_news", "мем", "живой стиль", "лайв", "дерзк", "как на скрине"],
            "classic_news": ["classic_news", "новостной пресет", "классический новост"],
            "tech_ai": ["tech_ai", "технолог", "ии стиль", "ai стиль"],
            "business": ["business", "деловой", "бизнес стиль"],
            "expert_blog": ["expert_blog", "эксперт", "авторский"],
        }
        if "пресет" in lowered or "стиль" in lowered:
            for preset_key, words in preset_words.items():
                if any(word in lowered for word in words):
                    return result("set_preset", 0.86, preset=preset_key)
        if source_url_match and any(w in lowered for w in ["rss", "источник", "добавь ссыл", "добавь источник"]):
            return result("add_source", 0.84, source_url=source_url_match.group(0).rstrip(".,)"))
        if pack_match and any(w in lowered for w in ["пакет", "источник", "подключ", "добав"]):
            return result("add_source_pack", 0.82, source_pack=pack_match.group(1))
        if "дайджест" in lowered or "подборк" in lowered:
            return result("create_digest", 0.86, topic=self._topic_without_channels(text))
        if any(w in lowered for w in ["новост", "инфоповод", "актуальн", "свеж"]):
            return result("create_news", 0.78, topic=self._topic_without_channels(text))
        if any(w in lowered for w in ["напиши", "сделай", "подготов", "пост", "опубликуй", "выложи"]):
            return result("create_prompt_post", 0.72, topic=self._topic_without_channels(text))
        return RouteResult(action="unknown", confidence=0.0, clarification="Не понял задачу. Напиши, например: «Сделай пост для @channel про ключевые события недели».")

    def _sanitize_channels(self, requested: list[str], channels: list[Channel], text: str, *, allow_explicit: bool) -> list[str]:
        available_by_username = {ch.username.lower(): ch.username for ch in channels}
        result: list[str] = []
        for raw in requested:
            name = _norm_channel(raw)
            if not name:
                continue
            if name in available_by_username:
                result.append(available_by_username[name])
            elif allow_explicit and re.search(r"@" + re.escape(name.lstrip("@")) + r"\b", text, flags=re.I):
                result.append(name)
        # Try fuzzy by title/username when user writes channel name without @.
        lowered = text.lower()
        for ch in channels:
            title = (ch.title or "").strip().lower()
            user_no_at = ch.username.lstrip("@").lower()
            if ch.username in result:
                continue
            if user_no_at and user_no_at in lowered:
                result.append(ch.username)
            elif title and len(title) >= 4 and title in lowered:
                result.append(ch.username)
        # If only one channel exists and task clearly targets a channel, allow inference.
        if not result and len(channels) == 1 and any(w in lowered for w in ["канал", "пост", "новост", "дайджест", "очеред", "календар", "промпт"]):
            result.append(channels[0].username)
        # Deduplicate preserving order.
        deduped: list[str] = []
        for item in result:
            if item not in deduped:
                deduped.append(item)
        return deduped

    @staticmethod
    def _topic_without_channels(text: str) -> str:
        text = re.sub(r"@[a-zA-Z0-9_]{4,64}", "", text)
        text = re.sub(r"\b(в|для|на)\s+канал[ае]?\b", "", text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip(" .,—|:")
        return text[:1000]
