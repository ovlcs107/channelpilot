from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy import func, select

from app.config import Settings
from app.database import session_scope
from app.models import AIResponseCache, AIUsageEvent, Channel
from app.services.ai_cost import (
    cache_cutoff,
    canonical_prompt,
    estimate_tokens_from_text,
    month_start_utc,
    prompt_hash,
    today_start_utc,
    utcnow_naive,
)
from app.services.news_collector import Article
from app.utils.text import truncate
from app.services.telegram_formatting import channel_format_mode
from app.services.style_presets import normalize_preset_key
from app.services.anti_cringe import sanitize_post_text
from app.services.post_formatter import normalize_channel_post_layout

logger = logging.getLogger(__name__)


class AIWriter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def write_news_post(
        self,
        channel: Channel,
        articles: list[Article],
        confidence: str,
        risk_notes: str,
        score_details: dict[str, object] | None = None,
        owner_telegram_id: int = 0,
        post_mode: str = "story",
    ) -> str:
        """Create a sourced Telegram post.

        post_mode="story" means one post = one clear news item. This is the
        safest default for normal channels. post_mode="digest" means a
        clearly labelled roundup of several accepted news items.
        """
        if not articles:
            raise ValueError("Нельзя создать новостной пост без источников.")
        post_mode = (post_mode or "story").strip().lower()
        if post_mode not in {"story", "digest"}:
            post_mode = "story"
        selected_articles = articles[:1] if post_mode == "story" else articles[:5]
        if self._should_skip_ai_for_news(channel, confidence, score_details or {}):
            return self._fallback_news_post(channel, selected_articles, confidence, risk_notes, post_mode=post_mode)
        prompt = self._build_news_prompt(channel, selected_articles, confidence, risk_notes, score_details or {}, post_mode=post_mode)
        response = await self._chat(prompt, purpose=f"news_{post_mode}", owner_telegram_id=owner_telegram_id)
        if response:
            return self._attach_sources(response, self._prompt_articles(channel, selected_articles), confidence, risk_notes)
        return self._fallback_news_post(channel, selected_articles, confidence, risk_notes, post_mode=post_mode)

    async def write_update_post(
        self,
        channel: Channel,
        article: Article,
        previous_title: str | None,
        confidence: str,
        risk_notes: str,
        owner_telegram_id: int = 0,
    ) -> str:
        prompt = self._build_update_prompt(channel, article, previous_title, confidence, risk_notes)
        response = await self._chat(prompt, purpose="update", owner_telegram_id=owner_telegram_id)
        if response:
            return self._attach_sources(response, [article], confidence, risk_notes)
        return self._fallback_update_post(article, previous_title, confidence, risk_notes)


    async def write_prompt_post(self, channel: Channel, task: str, owner_telegram_id: int = 0) -> str:
        """Create a non-news/editorial post using the channel's editorial policy.

        This is for expert/opinion/educational posts where the channel owner
        provides the topic. It must not invent fresh facts; news-like requests
        should be sent through /news so the source checker is used.
        """
        prompt = [
            {
                "role": "system",
                "content": (
                    "Ты главный редактор Telegram-канала. Работай строго по редакционной политике канала. "
                    "Это НЕ новостной режим: не придумывай свежие факты, даты, цифры, цитаты и заявления. "
                    "Если задача требует актуальных новостей или проверки фактов, прямо укажи, что нужен режим /news с источниками. "
                    "Верни только готовый пост без технических пояснений."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Канал: {channel.username}\n"
                    f"Тематика: {truncate(channel.topic, 600)}\n"
                    f"Стиль: {truncate(self._style_text(channel), 1000)}\n"
                    f"Редакционная политика / главный промпт канала:\n{truncate(channel.editorial_prompt or 'не задана, ориентируйся на тему и стиль', 3500)}\n\n"
                    f"Задача для поста:\n{truncate(task, 1600)}\n\n"
                    f"{self._formatting_instruction(channel)}\nСделай Telegram-пост на русском: сильное начало, короткие абзацы, без воды, без фейковых фактов, мягкий вывод или вопрос."
                ),
            },
        ]
        response = await self._chat(prompt, purpose="prompt_post", owner_telegram_id=owner_telegram_id)
        if response:
            result = sanitize_post_text(response).text if self.settings.anti_cringe_enabled else response
            return self._finalize_layout(result)
        return self._finalize_layout(
            f"{truncate(task, 180)}\n\n"
            "Короткая мысль по теме канала. Для фактов и свежих новостей лучше использовать режим /news, "
            "чтобы пост был основан на источниках, а не на фантазиях модели."
        )

    async def write_ad_post(self, channel: Channel, offer: str, link: str = "", notes: str = "", owner_telegram_id: int = 0) -> str:
        prompt = [
            {
                "role": "system",
                "content": (
                    "Ты редактор рекламных интеграций в Telegram. Пиши честный рекламный пост без обещаний, которых нет в ТЗ. "
                    "Не придумывай скидки, гарантии, цифры, отзывы и факты. Если данных мало — делай нейтрально. "
                    "Пиши компактно: сильное начало, 2-4 коротких абзаца, честный CTA."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Канал: {channel.username}\nТематика: {truncate(channel.topic, 500)}\nСтиль: {truncate(self._style_text(channel), 700)}\n"
                    f"Что рекламируем: {truncate(offer, 900)}\nСсылка: {truncate(link, 300) if link else 'нет'}\n"
                    f"Доп. заметки: {truncate(notes, 700) if notes else 'нет'}\n"
                    f"{self._formatting_instruction(channel)}\n\nВерни только готовый пост."
                ),
            },
        ]
        response = await self._chat(prompt, purpose="ad", owner_telegram_id=owner_telegram_id)
        if response:
            result = sanitize_post_text(response).text if self.settings.anti_cringe_enabled else response
            return self._finalize_layout(result)
        cta = f"\n\nСсылка: {link}" if link else ""
        return self._finalize_layout(
            f"{offer}\n\n"
            f"Коротко и по делу: это может быть полезно аудитории канала. {truncate(notes, 350) if notes else ''}\n\n"
            f"Посмотрите детали и решите сами — без магии и лишнего кликбейта.{cta}"
        )

    async def write_network_variant(self, channel: Channel, base_text: str, source_articles: list[Article], owner_telegram_id: int = 0) -> str:
        prompt = [
            {
                "role": "system",
                "content": (
                    "Ты редактор сетки Telegram-каналов. Адаптируй один инфоповод под конкретный канал. "
                    "Не добавляй новых фактов, работай только с базовым текстом и источниками. Пиши компактно."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Канал-получатель: {channel.username}\nТематика: {truncate(channel.topic, 500)}\nСтиль: {truncate(self._style_text(channel), 700)}\n\n"
                    f"Базовый пост:\n{truncate(base_text, 2500)}\n\n"
                    f"Источники JSON:\n{json.dumps([a.as_source_payload() for a in self._prompt_articles(channel, source_articles)], ensure_ascii=False)}\n\n"
                    f"{self._formatting_instruction(channel)}\nСделай отдельную версию под этот канал. Не добавляй блок источников — система добавит сама."
                ),
            },
        ]
        response = await self._chat(prompt, purpose="network", owner_telegram_id=owner_telegram_id)
        if response:
            return self._attach_sources(response, source_articles, "medium", "Сетевая адаптация создана по тем же источникам.")
        return self._attach_sources(base_text, source_articles, "medium", "Сетевая адаптация создана fallback-режимом.")

    async def rewrite_post(self, channel: Channel, original_text: str, instruction: str = "сделай лучше и живее", owner_telegram_id: int = 0) -> str:
        prompt = [
            {
                "role": "system",
                "content": (
                    "Ты редактор Telegram-канала. Перепиши текст по инструкции, не добавляй новые факты, "
                    "не выдумывай даты, цифры и цитаты. Верни только готовый текст поста."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Канал: {channel.username}\nТематика: {truncate(channel.topic, 500)}\nСтиль: {truncate(self._style_text(channel), 700)}\n"
                    f"Инструкция: {truncate(instruction, 300)}\n{self._formatting_instruction(channel)}\n\nИсходный пост:\n{truncate(original_text, 3500)}"
                ),
            },
        ]
        response = await self._chat(prompt, purpose="rewrite", owner_telegram_id=owner_telegram_id)
        result = response or original_text
        result = sanitize_post_text(result).text if self.settings.anti_cringe_enabled else result
        return self._finalize_layout(result)

    async def rewrite_as_media_post(self, channel: Channel, original_text: str, owner_telegram_id: int = 0) -> str:
        """Make an existing draft look like a clean editorial/media post."""
        return await self.rewrite_post(
            channel,
            original_text,
            (
                "оформи как пост нормального Telegram-СМИ: короткий сильный заголовок, "
                "одна понятная новость или явно подписанный дайджест, короткие абзацы, "
                "без канцелярита, без новых фактов, источники и ссылки не удаляй"
            ),
            owner_telegram_id=owner_telegram_id,
        )

    async def generate_ideas(self, channel: Channel, owner_telegram_id: int = 0) -> str:
        prompt = [
            {
                "role": "system",
                "content": "Ты SMM-стратег для Telegram. Предлагай практичные идеи без выдуманных новостей. Если идея требует фактов, помечай, что нужны источники. Пиши кратко.",
            },
            {
                "role": "user",
                "content": (
                    f"Сделай 12 идей постов для Telegram-канала {channel.username}.\n"
                    f"Тематика: {truncate(channel.topic, 500)}\nСтиль: {truncate(self._style_text(channel), 700)}\n"
                    "Формат: короткий список. Добавь 3 идеи для вовлечения аудитории."
                ),
            },
        ]
        response = await self._chat(prompt, purpose="ideas", owner_telegram_id=owner_telegram_id)
        if response:
            return response
        return (
            f"Идеи для {channel.username}:\n"
            "1. Дайджест главных новостей недели.\n2. Короткий разбор одного инфоповода.\n3. Опрос для аудитории.\n"
            "4. Подборка полезных ссылок.\n5. Пост 'что это значит для подписчиков'.\n6. Мнение автора по свежей теме.\n"
            "7. Топ-5 ошибок/мифов в нише.\n8. Мини-инструкция.\n9. Сравнение двух вариантов.\n"
            "10. Короткий прогноз.\n11. Лёгкий мемный пост.\n12. Пост с вопросом: 'а вы как думаете?'"
        )

    async def generate_plan(self, channel: Channel, owner_telegram_id: int = 0) -> str:
        prompt = [
            {"role": "system", "content": "Ты SMM-планировщик для Telegram. Не придумывай конкретные новости, составляй универсальный план. Пиши компактно."},
            {
                "role": "user",
                "content": (
                    f"Составь контент-план на 7 дней для канала {channel.username}.\n"
                    f"Тематика: {truncate(channel.topic, 500)}\nСтиль: {truncate(self._style_text(channel), 700)}\n"
                    "На каждый день дай 2-3 поста: тип поста, цель, короткое описание."
                ),
            },
        ]
        response = await self._chat(prompt, purpose="plan", owner_telegram_id=owner_telegram_id)
        if response:
            return response
        return (
            "Контент-план на 7 дней:\n\n"
            "День 1: новость + короткий разбор + опрос.\nДень 2: полезная подборка + мнение автора.\n"
            "День 3: мини-инструкция + вовлекающий вопрос.\nДень 4: дайджест + сравнение.\n"
            "День 5: выводы недели + лёгкий пост.\nДень 6: подборка ресурсов + обсуждение.\n"
            "День 7: итоги недели + планы на следующую неделю."
        )

    def _finalize_layout(self, text: str) -> str:
        return normalize_channel_post_layout(text)

    def _style_profile(self, channel: Channel) -> dict[str, Any]:
        try:
            profile = json.loads(channel.style_profile_json or "{}")
            return profile if isinstance(profile, dict) else {}
        except Exception:
            return {}

    def _formatting_instruction(self, channel: Channel) -> str:
        mode = channel_format_mode(channel, self.settings.post_format_default)
        profile = self._style_profile(channel)
        post_style = normalize_preset_key(str(profile.get("post_style") or profile.get("preset") or ""))
        if post_style == "meme_news":
            return (
                "Оформление Telegram-поста: строгая структура с воздухом. "
                "1) Первая строка — короткий заголовок в **жирном**. "
                "2) После заголовка всегда пустая строка. "
                "3) Далее 2-4 коротких абзаца по 1-2 предложения, между абзацами пустая строка. "
                "4) Можно добавить ОДИН цитатный блок через > как акцент. "
                "5) Можно ОДИН раз использовать ~~зачёркнутую иронию~~ или ||spoiler||, но только если это реально уместно. "
                "6) Не добавляй источник и CTA — система добавит футер сама. "
                "7) Не склеивай заголовок, текст и футер в одну простыню. "
                "Пиши обычным markdown-видом, не сырой HTML."
            )
        base_rules = (
            "Оформление Telegram-поста: первая строка — короткий заголовок в **жирном**; "
            "после заголовка пустая строка; основной текст дели на короткие абзацы по 1-2 предложения; "
            "между абзацами всегда пустая строка; можно использовать один > цитатный блок для ключевой мысли; "
            "не добавляй источник и CTA — система добавит футер сама; не склеивай всё в одну простыню. "
        )
        if mode == "html":
            return base_rules + "Пиши обычным markdown-видом (**заголовок**, > цитата), система безопасно переведёт его в Telegram HTML."
        if mode == "markdown_v2":
            return base_rules + "Пиши обычным markdown-видом, система безопасно конвертирует его в Telegram MarkdownV2."
        return base_rules + "Даже без форматирования сохраняй пустые строки между блоками."

    def _build_news_prompt(
        self,
        channel: Channel,
        articles: list[Article],
        confidence: str,
        risk_notes: str,
        score_details: dict[str, object],
        post_mode: str = "story",
    ) -> list[dict[str, str]]:
        article_payload: list[dict[str, Any]] = []
        summary_limit = self._summary_limit(channel)
        for index, article in enumerate(self._prompt_articles(channel, articles), start=1):
            article_payload.append(
                {
                    "id": index,
                    "title": truncate(article.title, 160),
                    "summary": truncate(article.summary, summary_limit),
                    "source": truncate(article.source_name, 80),
                    "domain": article.domain,
                    "published_at": article.published_at.isoformat() if article.published_at else None,
                    "url": article.url,
                }
            )
        compact_scores = self._compact_score_details(score_details)
        return [
            {
                "role": "system",
                "content": (
                    "Ты профессиональный редактор Telegram-канала. Пиши пост ТОЛЬКО по источникам. "
                    "Нельзя добавлять факты, даты, цифры, цитаты, имена и выводы, которых нет в источниках. "
                    "Если данных мало, пиши осторожно: 'по данным источника', 'сообщается', 'пока известно'. "
                    "Обязательно дай короткий понятный заголовок. Без кликбейта. "
                    "Если стиль канала просит живую подачу, можно использовать цитату/иронию, но факты всё равно только из источников. "
                    "Верни только готовый текст поста, без блока источников."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Канал: {channel.username}\nТематика: {truncate(channel.topic, 500)}\nСтиль: {truncate(self._style_text(channel), 700)}\n"
                    f"Уверенность: {confidence}\nРиск: {truncate(risk_notes, 450)}\n"
                    f"Режим поста: {post_mode}. Если story — один пост должен быть только про одну главную новость. "
                    f"Если digest — сделай понятный дайджест из нескольких новостей с короткими подзаголовками.\n"
                    f"Рейтинг JSON: {json.dumps(compact_scores, ensure_ascii=False)}\n\n"
                    f"{self._formatting_instruction(channel)}\n"
                    "Напиши Telegram-пост на русском языке. Для story: заголовок + 2-4 коротких абзаца + вопрос/вывод. "
                    "Для digest: общий заголовок + 3-5 коротких блоков, каждый блок с отдельным подзаголовком. "
                    "Не смешивай unrelated новости в один обычный story-пост.\n\n"
                    f"Источники JSON: {json.dumps(article_payload, ensure_ascii=False)}"
                ),
            },
        ]

    def _build_update_prompt(self, channel: Channel, article: Article, previous_title: str | None, confidence: str, risk_notes: str) -> list[dict[str, str]]:
        payload = {
            "title": truncate(article.title, 180),
            "summary": truncate(article.summary, self._summary_limit(channel)),
            "source": truncate(article.source_name, 80),
            "domain": article.domain,
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "updated_at": article.updated_at.isoformat() if article.updated_at else None,
            "url": article.url,
        }
        return [
            {
                "role": "system",
                "content": (
                    "Ты редактор Telegram-канала. Напиши короткое обновление к уже опубликованной новости. "
                    "Не добавляй факты вне источника. Текст должен звучать как reply/update."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Канал: {channel.username}\nСтиль: {truncate(self._style_text(channel), 700)}\n"
                    f"Старая новость: {previous_title or 'не указана'}\nУверенность: {confidence}\nРиск: {truncate(risk_notes, 450)}\n"
                    f"Новый источник JSON: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    f"{self._formatting_instruction(channel)}\nСделай короткий пост-обновление на русском. Не добавляй блок источников."
                ),
            },
        ]

    async def _chat(self, prompt: list[dict[str, str]], *, purpose: str, owner_telegram_id: int = 0) -> str:
        if not self.settings.ai_api_key:
            return ""
        prompt = self._limit_prompt(prompt)
        prompt_text = canonical_prompt(prompt)
        model = self._model_for_purpose(purpose)
        p_hash = prompt_hash(prompt_text, model=model, purpose=purpose)
        prompt_tokens = estimate_tokens_from_text(prompt_text)

        cached = await self._try_get_cache(p_hash, purpose, model, owner_telegram_id, prompt_tokens)
        if cached is not None:
            return cached
        if not await self._budget_allows(owner_telegram_id, prompt_tokens):
            logger.warning("AI budget exceeded for user %s, purpose=%s", owner_telegram_id, purpose)
            return ""

        url = self.settings.ai_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.ai_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.settings.ai_site_url,
            "X-Title": self.settings.ai_app_name,
        }
        payload = {
            "model": model,
            "messages": prompt,
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_output_tokens,
        }
        response_text = ""
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_request_timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                response_text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as exc:
            logger.warning("AI request failed, fallback will be used: %s", exc)
            await self._log_usage(owner_telegram_id, purpose, model, prompt_tokens, 0, cached=False)
            return ""

        completion_tokens = estimate_tokens_from_text(response_text) if response_text else 0
        await self._store_cache_and_usage(p_hash, purpose, model, response_text, prompt_tokens, completion_tokens, owner_telegram_id)
        return response_text

    async def _try_get_cache(self, p_hash: str, purpose: str, model: str, owner_telegram_id: int, prompt_tokens: int) -> str | None:
        if not self.settings.ai_cache_enabled:
            return None
        try:
            async with session_scope() as session:
                row = (
                    await session.execute(
                        select(AIResponseCache).where(
                            AIResponseCache.prompt_hash == p_hash,
                            AIResponseCache.created_at >= cache_cutoff(self.settings.ai_cache_ttl_hours),
                        )
                    )
                ).scalar_one_or_none()
                if row and row.response_text:
                    row.hits = (row.hits or 0) + 1
                    row.last_used_at = utcnow_naive()
                    await self._log_usage(owner_telegram_id, purpose, model, 0, 0, cached=True, session=session)
                    logger.info("AI cache hit purpose=%s model=%s saved_prompt_tokens_est=%s", purpose, model, prompt_tokens)
                    return row.response_text
        except Exception as exc:
            logger.debug("AI cache lookup failed: %s", exc)
        return None

    async def _budget_allows(self, owner_telegram_id: int, prompt_tokens: int) -> bool:
        if owner_telegram_id <= 0:
            return True
        if self.settings.ai_daily_token_budget <= 0 and self.settings.ai_monthly_token_budget <= 0:
            return True
        try:
            async with session_scope() as session:
                total_expr = func.coalesce(func.sum(AIUsageEvent.prompt_tokens_est + AIUsageEvent.completion_tokens_est), 0)
                daily = (await session.execute(select(total_expr).where(AIUsageEvent.owner_telegram_id == owner_telegram_id, AIUsageEvent.cached.is_(False), AIUsageEvent.created_at >= today_start_utc()))).scalar_one()
                monthly = (await session.execute(select(total_expr).where(AIUsageEvent.owner_telegram_id == owner_telegram_id, AIUsageEvent.cached.is_(False), AIUsageEvent.created_at >= month_start_utc()))).scalar_one()
            if self.settings.ai_daily_token_budget > 0 and int(daily or 0) + prompt_tokens > self.settings.ai_daily_token_budget:
                return False
            if self.settings.ai_monthly_token_budget > 0 and int(monthly or 0) + prompt_tokens > self.settings.ai_monthly_token_budget:
                return False
        except Exception as exc:
            logger.debug("AI budget check failed, allowing request: %s", exc)
        return True

    async def _store_cache_and_usage(self, p_hash: str, purpose: str, model: str, response: str, prompt_tokens: int, completion_tokens: int, owner_telegram_id: int) -> None:
        try:
            async with session_scope() as session:
                if self.settings.ai_cache_enabled and response:
                    row = (await session.execute(select(AIResponseCache).where(AIResponseCache.prompt_hash == p_hash))).scalar_one_or_none()
                    if row:
                        row.response_text = response
                        row.last_used_at = utcnow_naive()
                    else:
                        session.add(AIResponseCache(prompt_hash=p_hash, purpose=purpose, model=model, response_text=response, input_tokens_est=prompt_tokens, output_tokens_est=completion_tokens))
                await self._log_usage(owner_telegram_id, purpose, model, prompt_tokens, completion_tokens, cached=False, session=session)
        except Exception as exc:
            logger.debug("AI cache/usage save failed: %s", exc)

    async def _log_usage(self, owner_telegram_id: int, purpose: str, model: str, prompt_tokens: int, completion_tokens: int, *, cached: bool, session=None) -> None:
        event = AIUsageEvent(
            owner_telegram_id=owner_telegram_id or 0,
            purpose=purpose,
            model=model,
            prompt_tokens_est=prompt_tokens,
            completion_tokens_est=completion_tokens,
            cached=cached,
        )
        if session is not None:
            session.add(event)
            return
        async with session_scope() as new_session:
            new_session.add(event)

    def _limit_prompt(self, prompt: list[dict[str, str]]) -> list[dict[str, str]]:
        prompt_text = canonical_prompt(prompt)
        if len(prompt_text) <= self.settings.ai_max_input_chars:
            return prompt
        result = [dict(item) for item in prompt]
        overflow = len(prompt_text) - self.settings.ai_max_input_chars
        for item in reversed(result):
            content = item.get("content", "")
            if len(content) > overflow + 500:
                item["content"] = truncate(content, max(500, len(content) - overflow - 50))
                break
        return result

    def _prompt_articles(self, channel: Channel, articles: list[Article]) -> list[Article]:
        mode = self._cost_mode(channel)
        if mode == "quality":
            count = min(len(articles), max(4, self.settings.ai_prompt_max_articles, 5))
        elif mode == "balanced":
            count = min(len(articles), min(max(3, self.settings.ai_prompt_max_articles), 4))
        else:
            count = min(len(articles), min(self.settings.ai_prompt_max_articles, 2))
        return articles[: max(1, count)]

    def _summary_limit(self, channel: Channel) -> int:
        mode = self._cost_mode(channel)
        if mode == "quality":
            return max(650, self.settings.ai_source_summary_chars)
        if mode == "balanced":
            return min(max(360, self.settings.ai_source_summary_chars), 520)
        return min(self.settings.ai_source_summary_chars, 260)

    def _model_for_purpose(self, purpose: str) -> str:
        """Route cheap tasks to cheap models and quality tasks to better ones.

        Keeps backward compatibility with AI_MODEL / AI_FAST_MODEL while allowing
        Railway variables like AI_ROUTER_MODEL, AI_DRAFT_MODEL and AI_REWRITE_MODEL.
        """
        default = self.settings.ai_default_model or self.settings.ai_model
        if purpose in {"router", "natural_router"}:
            return self.settings.ai_router_model or self.settings.ai_fast_model or default
        if purpose.startswith("news") or purpose in {"update", "prompt_post", "network", "ad"}:
            return self.settings.ai_draft_model or default
        if purpose in {"rewrite", "ideas", "plan"}:
            return self.settings.ai_rewrite_model or self.settings.ai_fast_model or default
        if purpose in {"review", "editorial_review"}:
            return self.settings.ai_review_model or self.settings.ai_fast_model or default
        if purpose in {"quality", "premium"}:
            return self.settings.ai_quality_model or default
        if self.settings.cost_saver_enabled and purpose in {"rewrite", "ideas", "plan", "ad", "network"}:
            return self.settings.ai_fast_model or default
        return default

    def _should_skip_ai_for_news(self, channel: Channel, confidence: str, score_details: dict[str, object]) -> bool:
        if not self.settings.cost_saver_enabled:
            return False
        if self._cost_mode(channel) != "cheap":
            return False
        top_score = int(score_details.get("top_score") or 0) if isinstance(score_details, dict) else 0
        return confidence == "low" or (top_score and top_score < 55)

    def _compact_score_details(self, score_details: dict[str, object]) -> dict[str, object]:
        if not isinstance(score_details, dict):
            return {}
        scores = score_details.get("scores")
        if isinstance(scores, list):
            scores = scores[:3]
        return {"top_score": score_details.get("top_score", 0), "scores": scores or []}

    def _cost_mode(self, channel: Channel) -> str:
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        mode = str(profile.get("cost_mode") or self.settings.cost_saver_mode or "cheap").strip().lower()
        return mode if mode in {"cheap", "balanced", "quality"} else "cheap"

    def _fallback_news_post(self, channel: Channel, articles: list[Article], confidence: str, risk_notes: str, post_mode: str = "story") -> str:
        profile = self._style_profile(channel)
        post_style = normalize_preset_key(str(profile.get("post_style") or profile.get("preset") or ""))
        markdown_mode = channel_format_mode(channel, self.settings.post_format_default) == "markdown_v2"

        def bold(value: str) -> str:
            return f"**{value}**" if markdown_mode else f"<b>{value}</b>"

        def quote(value: str) -> str:
            value = truncate(value, 180)
            return f"> {value}" if markdown_mode else f"<blockquote>{value}</blockquote>"

        if post_mode == "digest" and len(articles) > 1:
            blocks = []
            for article in articles[:5]:
                blocks.append(
                    f"{bold(article.title)}\n"
                    f"{truncate(article.summary, 300) if article.summary else 'Подробности доступны в источнике.'}"
                )
            title = "Короткий дайджест" if post_style != "meme_news" else "Что сегодня шумит"
            body = f"{bold(title)}\n\n" + "\n\n".join(blocks)
            return self._attach_sources(body, articles, confidence, risk_notes)
        top = articles[0]
        summary = truncate(top.summary, 520) if top.summary else "подробности доступны по ссылке источника."
        if post_style == "meme_news":
            body = (
                f"{bold(top.title)}\n\n"
                f"{quote('не, ну это уже похоже на отдельный сюжет')}\n\n"
                f"Появился новый инфоповод. Коротко: {summary}\n\n"
                "Смотрим аккуратно: если появятся уточнения, лучше обновить пост отдельным reply."
            )
        else:
            body = (
                f"{bold(top.title)}\n\n"
                f"Появился новый инфоповод по теме канала. "
                f"Коротко: {summary}\n\n"
                "Пока лучше воспринимать это аккуратно и смотреть на подтверждения от других источников."
            )
        return self._attach_sources(body, articles, confidence, risk_notes)

    def _fallback_update_post(self, article: Article, previous_title: str | None, confidence: str, risk_notes: str) -> str:
        body = (
            "Обновление к новости.\n\n"
            f"Появились уточнения по теме: {article.title}.\n\n"
            f"Коротко: {truncate(article.summary, 500) if article.summary else 'детали доступны по ссылке источника.'}\n\n"
            "Фиксирую это как обновление к предыдущему сообщению, без лишних фантазий."
        )
        return self._attach_sources(body, [article], confidence, risk_notes)

    def _attach_sources(self, text: str, articles: list[Article], confidence: str, risk_notes: str) -> str:
        """Attach a public footer without exposing raw source URLs by default.

        Real source URLs are still persisted in the draft metadata. The channel
        post should look like a normal Telegram media post, so default output is
        a clean subscription CTA instead of a technical source block.
        """
        base = text.strip()
        if self.settings.anti_cringe_enabled:
            base = sanitize_post_text(base).text
        footer_mode = (self.settings.source_footer_mode or "brand_cta").strip().lower()

        # Optional old/debug behavior for internal testing only.
        if footer_mode == "sources" or self.settings.post_source_debug_enabled:
            if not articles:
                return base
            if len(articles) == 1:
                article = articles[0]
                source_block = f"🔗 Источник: {article.source_name or article.domain} — {article.url}"
            else:
                source_lines = [
                    f"{index}. {article.source_name or article.domain}: {article.title} — {article.url}"
                    for index, article in enumerate(articles[:5], start=1)
                ]
                source_block = "🔗 Источники:\n" + "\n".join(source_lines)
            debug_tail = ""
            if self.settings.post_source_debug_enabled:
                debug_tail = f"\n\nУверенность: {confidence}\nПроверка: {risk_notes}"
            return self._finalize_layout(f"{base}\n\n{source_block}{debug_tail}")

        footer = self._brand_cta_footer() if footer_mode == "brand_cta" else ""
        if footer:
            return self._finalize_layout(f"{base}\n\n{footer}")
        return self._finalize_layout(base)

    def _brand_cta_footer(self) -> str:
        if not self.settings.brand_cta_enabled:
            return ""
        label = (self.settings.brand_cta_label or "").strip()
        url = (self.settings.brand_cta_url or "").strip()
        prefix = (self.settings.brand_cta_prefix or "Подписаться на").strip()
        if not label:
            return ""
        if url.startswith("https://") or url.startswith("http://") or url.startswith("tg://"):
            return f"{prefix} [{label}]({url})"
        return f"{prefix} {label}"

    @staticmethod
    def _style_text(channel: Channel) -> str:
        base = channel.style
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        parts = [base]
        editorial = str(getattr(channel, "editorial_prompt", "") or "").strip()
        if editorial:
            parts.append(f"редакционная политика: {truncate(editorial, 1200)}")
        if not profile:
            return "; ".join(part for part in parts if part)
        for key, label in [
            ("tone", "тон"),
            ("emoji", "эмодзи"),
            ("length", "длина"),
            ("forbidden", "запрещено"),
            ("cta", "CTA"),
            ("formatting", "форматирование"),
            ("cost_mode", "экономия AI"),
            ("post_style", "стиль поста"),
            ("preset", "пресет"),
            ("source_policy", "политика источников"),
        ]:
            value = str(profile.get(key) or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        return "; ".join(parts)
