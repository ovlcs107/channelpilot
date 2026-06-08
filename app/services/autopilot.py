from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import AutopilotRun, Channel, Draft, PublishedPost, Source, SourceRule
from app.services.ai_writer import AIWriter
from app.services.news_collector import Article, NewsCollector
from app.services.publisher import Publisher
from app.services.source_checker import SourceChecker, normalize_domain
from app.utils.text import split_long_message
from app.bot.keyboards import draft_keyboard

logger = logging.getLogger(__name__)


def _source_json(articles: list[Article]) -> str:
    return json.dumps([article.as_source_payload() for article in articles], ensure_ascii=False)


def _draft_from_articles(
    channel: Channel,
    owner_telegram_id: int,
    text: str,
    articles: list[Article],
    confidence: str,
    risk_notes: str,
    kind: str = "news",
    reply_to_message_id: int | None = None,
    quality_score: int = 0,
    score_details: dict[str, object] | None = None,
) -> Draft:
    primary = articles[0] if articles else None
    return Draft(
        channel_id=channel.id,
        owner_telegram_id=owner_telegram_id,
        kind=kind,
        text=text,
        sources_json=_source_json(articles),
        media_url=primary.media_url if primary else None,
        media_type=primary.media_type if primary else None,
        reply_to_message_id=reply_to_message_id,
        primary_article_key=primary.article_key if primary else None,
        primary_article_topic_key=primary.topic_key if primary else None,
        primary_article_url=primary.url if primary else None,
        primary_article_title=primary.title if primary else None,
        primary_article_hash=primary.content_hash if primary else None,
        confidence=confidence,
        risk_notes=risk_notes,
        quality_score=quality_score,
        score_details_json=json.dumps(score_details or {}, ensure_ascii=False),
    )


async def _latest_exact_published(session: AsyncSession, channel_id: int, article: Article) -> PublishedPost | None:
    return (
        await session.execute(
            select(PublishedPost)
            .where(PublishedPost.channel_id == channel_id, PublishedPost.article_key == article.article_key)
            .order_by(desc(PublishedPost.published_at), desc(PublishedPost.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_similar_published(session: AsyncSession, channel_id: int, article: Article, window_days: int = 14) -> PublishedPost | None:
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    return (
        await session.execute(
            select(PublishedPost)
            .where(
                PublishedPost.channel_id == channel_id,
                or_(PublishedPost.article_key == article.article_key, PublishedPost.topic_key == article.topic_key),
                PublishedPost.published_at >= cutoff,
            )
            .order_by(desc(PublishedPost.published_at), desc(PublishedPost.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _rule_sets(session: AsyncSession, channel: Channel) -> dict[str, set[str]]:
    rules = (await session.execute(select(SourceRule).where(SourceRule.channel_id == channel.id))).scalars().all()
    result = {"block": set(), "trusted": set(), "priority": set(), "suspicious": set(), "allow": set()}
    for rule in rules:
        result.setdefault(rule.rule_type, set()).add(normalize_domain(rule.domain))
    return result


from app.services.autopilot_window import due_slots_for_channel


class AutopilotService:
    def __init__(self, settings: Settings, bot: Bot, collector: NewsCollector, checker: SourceChecker, writer: AIWriter, publisher: Publisher) -> None:
        self.settings = settings
        self.bot = bot
        self.collector = collector
        self.checker = checker
        self.writer = writer
        self.publisher = publisher

    async def tick(self, session: AsyncSession) -> None:
        now = datetime.now(ZoneInfo(self.settings.app_timezone))
        channels = (
            await session.execute(
                select(Channel)
                .options(selectinload(Channel.owner))
                .where(Channel.autopilot_enabled.is_(True), Channel.mode.in_(["safe", "semi", "auto"]))
            )
        ).scalars().all()

        logger.info(
            "Autopilot tick: now=%s timezone=%s enabled_channels=%s window_minutes=%s",
            now.strftime("%Y-%m-%d %H:%M:%S"),
            self.settings.app_timezone,
            len(channels),
            self.settings.autopilot_slot_window_minutes,
        )

        for channel in channels:
            due_slots = due_slots_for_channel(channel, now, self.settings.autopilot_slot_window_minutes)
            if not due_slots:
                logger.debug(
                    "Autopilot skip channel=%s: no due slot now. daily_times=%s mode=%s verified=%s",
                    channel.username,
                    channel.daily_times,
                    channel.mode,
                    channel.is_verified,
                )
                continue

            for slot in due_slots:
                existing = (
                    await session.execute(
                        select(AutopilotRun).where(AutopilotRun.channel_id == channel.id, AutopilotRun.slot_key == slot.slot_key)
                    )
                ).scalar_one_or_none()
                if existing:
                    logger.debug("Autopilot skip channel=%s slot=%s: already processed", channel.username, slot.slot_key)
                    continue

                run = AutopilotRun(channel_id=channel.id, slot_key=slot.slot_key, status="started")
                session.add(run)
                await session.flush()

                try:
                    logger.info(
                        "Autopilot run started: channel=%s slot=%s late=%ss mode=%s",
                        channel.username,
                        slot.slot_key,
                        slot.lateness_seconds,
                        channel.mode,
                    )
                    draft = await self.run_for_channel(session, channel)
                    run.status = "draft" if draft else "skipped"
                    logger.info("Autopilot run finished: channel=%s slot=%s status=%s", channel.username, slot.slot_key, run.status)
                except Exception as exc:
                    run.status = "failed"
                    logger.exception("Autopilot run failed: channel=%s slot=%s: %s", channel.username, slot.slot_key, exc)
                    await self._notify_owner(channel, f"Автопилот упал для {channel.username}: {exc}")

    async def run_for_channel(self, session: AsyncSession, channel: Channel) -> Draft | None:
        sources = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.enabled.is_(True)))).scalars().all()
        if not sources:
            await self._notify_owner(channel, f"Автопилот не создал пост для {channel.username}: не подключены источники. Добавьте RSS-пакет через /addpack или источник через /source.")
            return None

        articles = await self.collector.collect(channel, sources)
        logger.info("Autopilot collected articles: channel=%s sources=%s articles=%s", channel.username, len(sources), len(articles))
        if not articles:
            await self._notify_owner(channel, f"Автопилот не создал пост для {channel.username}: источники подключены, но свежих материалов не найдено.")
            return None

        rules = await _rule_sets(session, channel)
        check = self.checker.check(
            articles,
            min_sources=channel.min_sources,
            blocked_domains=rules.get("block"),
            trusted_domains=rules.get("trusted"),
            priority_domains=rules.get("priority"),
            suspicious_domains=rules.get("suspicious"),
        )
        logger.info(
            "Autopilot source check: channel=%s accepted=%s top_score=%s min_score=%s confidence=%s",
            channel.username,
            len(check.accepted),
            check.top_score,
            self.settings.autopilot_min_score,
            check.confidence,
        )
        if not check.accepted:
            await self._notify_owner(channel, f"Автопилот не создал пост для {channel.username}: свежие разрешённые источники не найдены. Проверьте /sourcecheck {channel.username} тема")
            return None
        if check.top_score < self.settings.autopilot_min_score:
            await self._notify_owner(channel, f"Автопилот пропустил пост для {channel.username}: рейтинг {check.top_score}/100 ниже лимита {self.settings.autopilot_min_score}. Можно снизить AUTOPILOT_MIN_SCORE или добавить более подходящие источники.")
            return None

        if channel.auto_reply_updates:
            update_draft = await self._try_create_update_draft(session, channel, check.accepted, check.confidence, check.risk_notes, check.top_score, check.score_payload())
            if update_draft:
                await self._handle_draft_by_mode(session, channel, update_draft)
                return update_draft

        fresh_articles: list[Article] = []
        duplicate_count = 0
        for article in check.accepted:
            previous = await _latest_similar_published(session, channel.id, article, self.settings.duplicate_topic_window_days)
            if not previous:
                fresh_articles.append(article)
            else:
                duplicate_count += 1
        if not fresh_articles:
            await self._notify_owner(channel, f"Автопилот пропустил слот для {channel.username}: все найденные темы уже публиковались за последние {self.settings.duplicate_topic_window_days} дней. Дублей: {duplicate_count}.")
            return None

        text = await self.writer.write_news_post(channel, fresh_articles, check.confidence, check.risk_notes, check.score_payload(), owner_telegram_id=channel.owner.telegram_id)
        draft = _draft_from_articles(channel, channel.owner.telegram_id, text, fresh_articles, check.confidence, check.risk_notes, quality_score=check.top_score, score_details=check.score_payload())
        session.add(draft)
        await session.flush()
        await self._handle_draft_by_mode(session, channel, draft)
        return draft

    async def _try_create_update_draft(self, session: AsyncSession, channel: Channel, articles: list[Article], confidence: str, risk_notes: str, score: int, score_details: dict[str, object]) -> Draft | None:
        for article in articles:
            previous = await _latest_exact_published(session, channel.id, article)
            if previous and previous.content_hash and previous.content_hash != article.content_hash:
                text = await self.writer.write_update_post(channel, article, previous.article_title, confidence, risk_notes, owner_telegram_id=channel.owner.telegram_id)
                draft = _draft_from_articles(channel, channel.owner.telegram_id, text, [article], confidence, risk_notes, kind="update", reply_to_message_id=previous.message_id, quality_score=score, score_details=score_details)
                session.add(draft)
                await session.flush()
                return draft
        return None

    async def _handle_draft_by_mode(self, session: AsyncSession, channel: Channel, draft: Draft) -> None:
        if channel.mode == "auto" and draft.confidence in {"medium", "high"} and draft.quality_score >= self.settings.autopilot_min_score:
            await self.publisher.publish_draft(session, draft)
            suffix = " ответом на старую новость" if draft.reply_to_message_id else ""
            await self._notify_owner(channel, f"Автопилот опубликовал черновик #{draft.id} в {channel.username}{suffix}. Рейтинг: {draft.quality_score}/100")
        else:
            meta = [
                f"Канал: {channel.username}",
                f"Режим: {channel.mode}",
                f"Рейтинг: {draft.quality_score}/100",
                "Публикация: ожидает ручного подтверждения" if channel.mode != "auto" else "Публикация: auto не прошёл условия безопасности",
            ]
            if draft.media_url:
                meta.append(f"Медиа: {draft.media_type or 'photo'}")
            if draft.reply_to_message_id:
                meta.append(f"Ответом на message_id={draft.reply_to_message_id}")
            await self._notify_owner_draft(
                channel,
                draft.id,
                f"Автопилот подготовил черновик #{draft.id}:\n" + "\n".join(meta) + f"\n\n{draft.text}",
            )

    async def _notify_owner(self, channel: Channel, text: str) -> None:
        for chunk in split_long_message(text):
            try:
                await self.bot.send_message(channel.owner.telegram_id, chunk, parse_mode=None)
            except Exception as exc:
                logger.warning("Failed to notify owner %s: %s", channel.owner.telegram_id, exc)

    async def _notify_owner_draft(self, channel: Channel, draft_id: int, text: str) -> None:
        chunks = split_long_message(text)
        for chunk in chunks[:-1]:
            try:
                await self.bot.send_message(channel.owner.telegram_id, chunk, parse_mode=None)
            except Exception as exc:
                logger.warning("Failed to notify owner %s: %s", channel.owner.telegram_id, exc)
        try:
            await self.bot.send_message(
                channel.owner.telegram_id,
                chunks[-1] if chunks else f"Автопилот подготовил черновик #{draft_id}.",
                parse_mode=None,
                reply_markup=draft_keyboard(draft_id),
            )
        except Exception as exc:
            logger.warning("Failed to notify owner %s with draft keyboard: %s", channel.owner.telegram_id, exc)
