from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.keyboards import draft_keyboard, menu_keyboard, publish_confirm_keyboard
from app.bot.messages import HELP_TEXT, START_TEXT
from app.config import Settings
from app.database import session_scope
from app.models import AIUsageEvent, AuditEvent, AutopilotRun, Channel, Draft, PartnerDeal, PublishedPost, ScheduledPost, Source, SourceRule, User, SecurityEvent
from app.services.ai_writer import AIWriter
from app.services.news_collector import Article, NewsCollector
from app.services.publisher import Publisher
from app.services.ai_router import NaturalLanguageRouter, RouteResult
from app.services.source_checker import SourceChecker, normalize_domain
from app.services.source_packs import SourcePackRegistry
from app.utils.text import split_long_message
from app.services.telegram_formatting import (
    FORMAT_HTML,
    channel_format_mode,
    format_ui_html,
    format_for_telegram,
    normalize_format_mode,
    parse_mode_for_format,
    strip_telegram_formatting,
    format_dm_markdown,
)
from app.utils.url_safety import is_safe_public_url
from app.services.ai_cost import month_start_utc, today_start_utc
from app.services.style_presets import apply_preset_to_channel, get_preset, list_preset_lines
from app.services.editorial_review import review_post_text
from app.services.audit import add_audit_event
from app.services.backup import export_channel_config
from app.services.content_day import build_day_plan
from app.services.launch_kit import commercial_offer_text, demo_script_text, launch_checklist_text, pricing_text, setup_master_text
from app.services.anti_cringe import sanitize_post_text, detect_ai_smells
from app.services.media_relevance import choose_safe_article_media
from app.services.autopilot import AutopilotService
from app.services.autopilot_window import due_slots_for_channel
from app.services.tenant_guard import (
    PublicSafetyError,
    ensure_channel_claimable,
    ensure_channel_limit,
    ensure_daily_draft_limit,
    ensure_network_size,
    ensure_public_auto_allowed,
    ensure_schedule_limit,
    ensure_source_limit,
    is_system_admin,
)

logger = logging.getLogger(__name__)
router = Router()


class HandlerDeps:
    def __init__(self, settings: Settings, bot: Bot, collector: NewsCollector, checker: SourceChecker, writer: AIWriter, publisher: Publisher) -> None:
        self.settings = settings
        self.bot = bot
        self.collector = collector
        self.checker = checker
        self.writer = writer
        self.publisher = publisher


_deps: HandlerDeps | None = None


def setup_handlers(deps: HandlerDeps) -> Router:
    global _deps
    _deps = deps
    return router


def deps() -> HandlerDeps:
    if _deps is None:
        raise RuntimeError("Handlers are not configured")
    return _deps




def user_role(settings: Settings, user: User | None, telegram_id: int) -> str:
    if telegram_id in settings.admin_ids:
        return "owner"
    role = (getattr(user, "role", "") or "").strip().lower() if user else ""
    if role in {"owner", "admin"}:
        return "owner"
    if role in {"operator", "viewer"}:
        return role
    return "viewer"

def role_can(role: str, action: str) -> bool:
    role = (role or "viewer").lower()
    if role == "owner":
        return True
    if role == "operator":
        return action in {"draft", "news", "digest", "prompt", "rewrite", "schedule", "publish", "view"}
    return action in {"view"}

def public_post_sources_visible() -> bool:
    settings = deps().settings
    return (settings.source_footer_mode or "brand_cta").strip().lower() == "sources" or settings.post_source_debug_enabled


async def get_or_create_user(session: AsyncSession, message: Message) -> User:
    from_user = message.from_user
    if from_user is None:
        raise ValueError("Не удалось определить пользователя.")
    user = (await session.execute(select(User).where(User.telegram_id == from_user.id))).scalar_one_or_none()
    if user:
        user.username = from_user.username
        user.full_name = from_user.full_name
        return user
    user = User(telegram_id=from_user.id, username=from_user.username, full_name=from_user.full_name)
    session.add(user)
    await session.flush()
    return user


def command_args(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def normalize_channel_username(value: str) -> str:
    value = value.strip().lower()
    if not value:
        return value
    if not value.startswith("@"):
        value = "@" + value
    return value


def channel_allowed_or_text(username: str) -> tuple[bool, str]:
    settings = deps().settings
    if settings.is_channel_allowed(username):
        return True, ""
    allowed = ", ".join(settings.allowed_channels)
    return False, f"Канал {username} не входит в ALLOWED_CHANNELS. Этот private-проект настроен только на утверждённые каналы: {allowed}"


async def find_owned_channel(session: AsyncSession, owner_tg_id: int, username: str) -> Optional[Channel]:
    username = normalize_channel_username(username)
    result = await session.execute(
        select(Channel)
        .join(User)
        .options(selectinload(Channel.owner), selectinload(Channel.sources), selectinload(Channel.rules))
        .where(User.telegram_id == owner_tg_id, Channel.username == username)
    )
    return result.scalar_one_or_none()


async def find_channel_any_owner(session: AsyncSession, username: str) -> Optional[Channel]:
    username = normalize_channel_username(username)
    result = await session.execute(
        select(Channel)
        .options(selectinload(Channel.owner))
        .where(Channel.username == username)
        .order_by(Channel.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def require_public_safety(action):
    try:
        return await action()
    except PublicSafetyError as exc:
        return str(exc)



async def require_safe_owned_channel_for_user(
    session: AsyncSession,
    username: str,
    telegram_id: int,
    *,
    require_verified: bool = True,
) -> tuple[Channel | None, str]:
    """Return a channel only if it belongs to this user and passes live Telegram checks.

    This is the main guard against mixing channels in personal/private mode:
    DB ownership, ALLOWED_CHANNELS, bot posting rights, and human admin rights
    are all checked before prompt changes, draft generation, and publication.
    """
    username = normalize_channel_username(username)
    allowed, reason = channel_allowed_or_text(username)
    if not allowed:
        return None, reason
    channel = await find_owned_channel(session, telegram_id, username)
    if not channel:
        return None, f"Канал {username} не привязан к аккаунту администратора. Сначала /addchannel {username} | тема | стиль"
    ok, info = await verify_channel_for_user(username, telegram_id)
    channel.is_verified = ok
    if ok:
        channel.title = info
    if not ok:
        return None, f"Проверка канала {username} не пройдена: {info}. Бот и пользователь должны быть администраторами этого канала."
    if require_verified and deps().settings.require_channel_verification and not channel.is_verified:
        return None, f"Канал {username} не verified. Выполни /check {username}"
    return channel, "ok"


def parse_channel_list(value: str) -> list[str]:
    return [normalize_channel_username(item) for item in value.split(",") if item.strip()]


async def reply_md(message: Message, markdown_text: str, **kwargs) -> None:
    """Reply in Telegram MarkdownV2 using safe renderer and plain fallback.

    Telegram Markdown is fragile: one unescaped symbol from generated/help text
    can break the whole response. This wrapper is the only safe way to send
    bot UI messages written in normal markdown. If Telegram still rejects the
    formatted version, the same text is sent without parse_mode so handlers
    never crash on user-facing messages.
    """
    parse_mode = "MarkdownV2" if deps().settings.dm_parse_mode.lower().replace("-", "_") == "markdown_v2" else None
    if parse_mode:
        try:
            await message.answer(format_dm_markdown(markdown_text), parse_mode=parse_mode, **kwargs)
            return
        except Exception as exc:
            logger.warning("MarkdownV2 DM send failed, falling back to plain text: %s", exc)
    await message.answer(strip_telegram_formatting(markdown_text), **kwargs)


async def load_user_channels(session: AsyncSession, telegram_id: int) -> list[Channel]:
    rows = (
        await session.execute(
            select(Channel)
            .join(User)
            .options(selectinload(Channel.sources), selectinload(Channel.rules))
            .where(User.telegram_id == telegram_id)
            .order_by(Channel.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def create_news_or_digest_draft(message: Message, username: str, query: str, *, digest_mode: bool = False) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    if digest_mode:
        await reply_md(message, "🔎 **Собираю дайджест**\n\nИщу несколько разных свежих инфоповодов, проверяю источники и антидубли.")
    else:
        await reply_md(message, "🔎 **Ищу одну сильную новость**\n\nПроверяю источники, медиа, рейтинг и дубли. Пост уйдёт только в черновик.")
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await reply_md(message, f"⚠️ **Не могу работать с каналом**\n\n{error}")
            return
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings)
        except PublicSafetyError as exc:
            await reply_md(message, f"⚠️ **Лимит черновиков**\n\n{exc}")
            return
        check = await _collect_checked(session, channel, query=query, min_sources=1)
        if not check.accepted:
            await reply_md(message, "⚠️ **Свежих проверенных источников не нашёл**\n\nПост не создан — это защита от новостей из воздуха.\n\nЧто можно сделать:\n• добавить пакет источников: `/addpack @channel ru_world`\n• проверить источники: `/sourcecheck @channel тема`\n• сделать не новость, а редакционный пост: `Сделай пост для @channel про ...`")
            return
        fresh = await _filter_new_topics(session, channel, check.accepted)
        if digest_mode:
            if len(fresh) < 2:
                await reply_md(message, "⚠️ **Для дайджеста мало разных новостей**\n\nМожно сделать одну новость вместо дайджеста.")
                return
            selected = fresh[:5]
            post_mode = "digest"
            kind = "digest"
        else:
            if not fresh:
                await reply_md(message, "♻️ **Похожая новость уже была**\n\nСработала защита от повторной публикации..")
                return
            selected = fresh[:1]
            post_mode = "story"
            kind = "news"
        text = await deps().writer.write_news_post(
            channel,
            selected,
            check.confidence,
            check.risk_notes,
            check.score_payload(),
            owner_telegram_id=message.from_user.id,
            post_mode=post_mode,
        )
        draft = _draft_from_articles(
            channel,
            message.from_user.id,
            text,
            selected,
            check.confidence,
            check.risk_notes,
            kind=kind,
            quality_score=check.top_score,
            score_details=check.score_payload(),
        )
        review = review_post_text(draft.text, require_source=public_post_sources_visible(), digest=digest_mode)
        if review.warnings:
            draft.risk_notes = (draft.risk_notes or "") + "\n" + review.summary()
        session.add(draft)
        await session.flush()
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_digest_created" if digest_mode else "natural_news_created", details=f"draft={draft.id}; channel={channel.username}; query={query[:500]}; review={review.score}"))
        preview = (draft.id, draft.text, draft.media_url, draft.media_type, draft.reply_to_message_id, draft.quality_score, channel.username)
    await send_draft_preview(message, *preview)


async def create_prompt_drafts_for_channels(message: Message, usernames: list[str], task: str) -> None:
    if message.from_user is None:
        return
    usernames = [normalize_channel_username(item) for item in usernames if item.strip()]
    if not usernames:
        await reply_md(message, "⚠️ **Не понял канал**\n\nУкажи, куда готовить пост: например `для @client_news`.")
        return
    max_channels = deps().settings.prompt_post_max_channels
    if max_channels > 0 and len(usernames) > max_channels and not is_system_admin(deps().settings, message.from_user.id):
        await reply_md(message, f"⚠️ **Слишком много каналов**\n\nЗа раз можно максимум {max_channels}. Так меньше риск перепутать сетку.")
        return
    try:
        ensure_network_size(message.from_user.id, usernames, deps().settings)
    except PublicSafetyError as exc:
        await reply_md(message, f"⚠️ **Лимит сетевого постинга**\n\n{exc}")
        return

    await reply_md(message, "🧠 **Готовлю редакционный пост**\n\nПроверяю каналы и применяю их главные промпты. Публикации без подтверждения не будет.")
    previews = []
    async with session_scope() as session:
        channels: list[Channel] = []
        for username in usernames:
            channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
            if not channel:
                await reply_md(message, f"⚠️ **Канал не прошёл проверку**\n\n{error}")
                return
            channels.append(channel)
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings, adding=len(channels))
        except PublicSafetyError as exc:
            await reply_md(message, f"⚠️ **Лимит черновиков**\n\n{exc}")
            return
        for channel in channels:
            text = await deps().writer.write_prompt_post(channel, task, owner_telegram_id=message.from_user.id)
            draft = Draft(
                channel_id=channel.id,
                owner_telegram_id=message.from_user.id,
                kind="prompt",
                text=text,
                confidence="manual",
                risk_notes="Редакционный пост создан по главному промпту канала. Для новостей и фактов используй новостной режим с источниками.",
            )
            review = review_post_text(draft.text, require_source=False, digest=False)
            if review.warnings:
                draft.risk_notes = (draft.risk_notes or "") + "\n" + review.summary()
            session.add(draft)
            await session.flush()
            session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_prompt_draft_created", details=f"draft={draft.id}; channel={channel.username}; task={task[:500]}; review={review.score}"))
            previews.append((draft.id, draft.text, None, None, None, 0, channel.username))
    for preview in previews:
        await send_draft_preview(message, *preview)


async def verify_channel_for_user(username: str, telegram_id: int) -> tuple[bool, str]:
    if deps().settings.public_require_user_channel_admin:
        return await deps().publisher.verify_channel_access(username, telegram_id)
    return await deps().publisher.verify_channel_permissions(username)


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
    reply_to_message_id: Optional[int] = None,
    quality_score: int = 0,
    score_details: dict[str, object] | None = None,
) -> Draft:
    primary = articles[0] if articles else None
    media_url = primary.media_url if primary else None
    media_type = primary.media_type if primary else None
    if primary and deps().settings.media_relevance_enabled:
        media_url, media_type, media_decision = choose_safe_article_media(primary)
        if not media_url:
            risk_notes = (risk_notes or "") + f"\nМедиа не прикреплено: {media_decision.reason} (score={media_decision.score})."
    return Draft(
        channel_id=channel.id,
        owner_telegram_id=owner_telegram_id,
        kind=kind,
        text=sanitize_post_text(text).text if deps().settings.anti_cringe_enabled else text,
        sources_json=_source_json(articles),
        media_url=media_url,
        media_type=media_type,
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


async def _latest_similar_published(session: AsyncSession, channel_id: int, article: Article) -> PublishedPost | None:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=deps().settings.duplicate_topic_window_days)
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


async def _find_update_candidate(session: AsyncSession, channel: Channel, articles: list[Article]) -> tuple[Article, PublishedPost] | None:
    for article in articles:
        previous = await _latest_exact_published(session, channel.id, article)
        if previous and previous.content_hash and previous.content_hash != article.content_hash:
            return article, previous
    return None


async def _filter_new_topics(session: AsyncSession, channel: Channel, articles: list[Article]) -> list[Article]:
    result: list[Article] = []
    for article in articles:
        previous = await _latest_similar_published(session, channel.id, article)
        if not previous:
            result.append(article)
    return result


async def _rule_sets(session: AsyncSession, channel: Channel) -> dict[str, set[str]]:
    rules = (await session.execute(select(SourceRule).where(SourceRule.channel_id == channel.id))).scalars().all()
    result = {"block": set(), "trusted": set(), "priority": set(), "suspicious": set(), "allow": set()}
    for rule in rules:
        result.setdefault(rule.rule_type, set()).add(normalize_domain(rule.domain))
    return result


async def _collect_checked(session: AsyncSession, channel: Channel, query: str, min_sources: int | None = None):
    sources = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.enabled.is_(True)))).scalars().all()
    articles = await deps().collector.collect(channel, sources, query=query)
    rules = await _rule_sets(session, channel)
    check = deps().checker.check(
        articles,
        min_sources=min_sources or channel.min_sources,
        blocked_domains=rules.get("block"),
        trusted_domains=rules.get("trusted"),
        priority_domains=rules.get("priority"),
        suspicious_domains=rules.get("suspicious"),
        query=query,
        channel_topic=channel.topic or "",
        editorial_prompt=channel.editorial_prompt or "",
    )
    return check


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
    user_id = message.from_user.id if message.from_user else "unknown"
    public_note = ""
    if deps().settings.bot_access_mode in {"public", "open"} and not user.accepted_terms_at:
        public_note = "\n\nДля публичного использования сначала нажми /accept — это защита от чужих каналов, спама и случайной публикации не туда."
    # /start uses safe Telegram HTML, not legacy Markdown.
    # If Telegram rejects formatting for any reason, fall back to plain text.
    start_text = f"{START_TEXT}{public_note}\n\nTelegram ID для ADMIN_IDS: {user_id}"
    try:
        await message.answer(format_ui_html(start_text), parse_mode="HTML")
    except Exception as exc:
        logger.warning("Safe HTML /start failed, falling back to plain text: %s", exc)
        await message.answer(strip_telegram_formatting(start_text), parse_mode=None)


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(
        "Правила публичного использования ChannelPilot AI:\n"
        "1. Подключайте только каналы, где у пользователя есть права администратора.\n"
        "2. Бот проверяет права пользователя и свои права публикации.\n"
        "3. Один @channel нельзя привязать к разным аккаунтам, чтобы никто случайно не управлял чужим каналом.\n"
        "4. Новостные посты должны основываться на источниках. За публикацию после подтверждения отвечает владелец канала.\n"
        "5. Запрещены спам, фишинг, вредоносные ссылки и попытки обойти лимиты.\n\n"
        "Принять: /accept"
    )


@router.message(Command("accept"))
async def cmd_accept(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        user.accepted_terms_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await message.answer("✅ Правила приняты. Теперь можно подключать канал: /quick @channel ru_world или /addchannel @channel | тема | стиль")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    settings = deps().settings
    ai_state = "подключён" if settings.ai_api_key else "не подключён, fallback-режим"
    search_parts = []
    if settings.newsapi_key:
        search_parts.append("NewsAPI")
    if settings.brave_search_api_key:
        search_parts.append("Brave Search")
    search_state = ", ".join(search_parts) if search_parts else "только RSS"
    await message.answer(
        "Статус:\n"
        f"AI: {ai_state}\nПоиск: {search_state}\nБаза: PostgreSQL\n"
        f"Медиа: {'скачивание включено' if settings.media_download_enabled else 'только ссылки'}\n"
        f"AI economy: {'on' if settings.cost_saver_enabled else 'off'} / {settings.cost_saver_mode}, cache={'on' if settings.ai_cache_enabled else 'off'}\n"
        f"Edition: {settings.project_edition}, project={settings.project_name}\n"
        f"Access: {settings.bot_access_mode}, rate limit={'on' if settings.rate_limit_enabled else 'off'}\n"
        f"Allowed channels: {', '.join(settings.allowed_channels) if settings.allowed_channels else 'любой проверенный канал админа'}\n"
        f"Public safety: terms={'on' if settings.public_require_terms_acceptance else 'off'}, user-admin-check={'on' if settings.public_require_user_channel_admin else 'off'}, auto-public={'on' if settings.public_allow_auto_publish else 'off'}\n"
        f"Антидубли по теме: {settings.duplicate_topic_window_days} дней\n"
        f"Часовой пояс: {settings.app_timezone}"
    )


@router.message(Command("addchannel", "add"))
async def cmd_add_channel(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /addchannel @channel | тема | стиль")
        return
    if message.from_user is None:
        return
    parts = [part.strip() for part in args.split("|")]
    username = normalize_channel_username(parts[0])
    allowed, reason = channel_allowed_or_text(username)
    if not allowed:
        await message.answer(reason)
        return
    topic = parts[1] if len(parts) > 1 and parts[1] else "новости и полезный контент"
    style = parts[2] if len(parts) > 2 and parts[2] else "коротко, понятно, без кликбейта"

    verified, verify_message = await verify_channel_for_user(username, message.from_user.id)
    if not verified and deps().settings.public_require_user_channel_admin and not is_system_admin(deps().settings, message.from_user.id):
        await message.answer("Канал не подключён: " + verify_message + "\n\nДобавьте бота администратором с правом публикации и убедитесь, что Telegram-аккаунт администратора тоже имеет права в этом канале. Затем повторите команду.")
        return
    title = verify_message if verified else None
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        try:
            await ensure_channel_claimable(session, user, username)
            channel = (await session.execute(select(Channel).where(Channel.owner_id == user.id, Channel.username == username))).scalar_one_or_none()
            if not channel:
                await ensure_channel_limit(session, user, deps().settings)
                channel = Channel(
                    owner_id=user.id,
                    username=username,
                    title=title,
                    topic=topic,
                    style=style,
                    min_sources=deps().settings.default_min_sources,
                    is_verified=verified,
                    media_enabled=deps().settings.media_enabled_by_default,
                )
                session.add(channel)
            else:
                channel.topic = topic
                channel.style = style
                channel.is_verified = verified
                channel.title = title or channel.title
            await session.flush()
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
    status = "✅ права проверены: бот может публиковать, пользователь является администратором канала" if verified else f"⚠️ канал сохранён, но проверка не пройдена: {verify_message}"
    await message.answer(f"Канал {username} подключён.\nТема: {topic}\nСтиль: {style}\nСтатус: {status}\n\nRSS: /source {username} https://site.com/rss")


@router.message(Command("check"))
async def cmd_check(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /check @channel")
        return
    if message.from_user is None:
        return
    username = normalize_channel_username(args.split()[0])
    allowed, reason = channel_allowed_or_text(username)
    if not allowed:
        await message.answer(reason)
        return
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if not channel:
            await message.answer("Этот канал не привязан к аккаунту администратора. Сначала /addchannel @channel | тема | стиль")
            return
    verified, info = await verify_channel_for_user(username, message.from_user.id)
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if channel:
            channel.is_verified = verified
            if verified:
                channel.title = info
    await message.answer(("✅ Всё нормально: " if verified else "⚠️ Есть проблема: ") + info)


@router.message(Command("channels"))
async def cmd_channels(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        rows = (await session.execute(select(Channel).options(selectinload(Channel.sources)).join(User).where(User.telegram_id == message.from_user.id).order_by(Channel.created_at.desc()))).scalars().all()
    if not rows:
        await message.answer("Каналов пока нет. Добавьте: /addchannel @channel | тема | стиль")
        return
    lines = [
        f"{ch.username} — verified={'yes' if ch.is_verified else 'no'}, prompt={'yes' if (ch.editorial_prompt or '').strip() else 'no'}, mode={ch.mode}, autopilot={'on' if ch.autopilot_enabled else 'off'}, media={'on' if ch.media_enabled else 'off'}, updates={'on' if ch.auto_reply_updates else 'off'}, sources={len(ch.sources)}"
        for ch in rows
    ]
    await message.answer("Подключённые каналы:\n" + "\n".join(lines))


@router.message(Command("source", "rss"))
async def cmd_source(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /source @channel https://site.com/rss")
        return
    username = normalize_channel_username(parts[0])
    url = parts[1].strip()
    ok, reason = is_safe_public_url(url, allow_localhost=not deps().settings.source_url_private_networks_blocked)
    if not ok:
        await message.answer(f"RSS-ссылка отклонена: {reason}")
        return
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Сначала подключи канал через /addchannel")
            return
        try:
            await ensure_source_limit(session, channel, deps().settings, adding=1)
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        source = Source(channel_id=channel.id, url=url)
        session.add(source)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            await message.answer("Этот источник уже добавлен.")
            return
    await message.answer(f"Источник добавлен для {username}:\n{url}")


@router.message(Command("sources"))
async def cmd_sources(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /sources @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден. Добавьте его через /addchannel")
            return
        sources = (await session.execute(select(Source).where(Source.channel_id == channel.id))).scalars().all()
    if not sources:
        await message.answer("Источников пока нет. Добавьте: /source @channel https://site.com/rss")
        return
    await message.answer("Источники:\n" + "\n".join(f"{i}. {src.url}" for i, src in enumerate(sources, 1)))




@router.message(Command("sourcecheck", "srcdebug"))
async def cmd_sourcecheck(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if len(parts) < 1 or not parts[0]:
        await message.answer("Формат: /sourcecheck @channel [тема]")
        return
    if message.from_user is None:
        return
    username = normalize_channel_username(parts[0])
    query = parts[1].strip() if len(parts) > 1 else ""
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if not channel:
            await message.answer("Канал не найден. Сначала подключите его через /addchannel")
            return
        check = await _collect_checked(session, channel, query=query, min_sources=1)
        source_count = (await session.execute(select(func.count(Source.id)).where(Source.channel_id == channel.id, Source.enabled.is_(True)))).scalar_one()
    lines = [
        f"Диагностика источников для {username}",
        f"Активных RSS: {int(source_count or 0)}",
        f"Принято материалов: {len(check.accepted)}",
        f"Отклонено: {check.rejected_count}",
        f"Уверенность: {check.confidence}",
        f"Пояснение: {check.risk_notes}",
        "",
        "Топ материалов:",
    ]
    if not check.scores:
        lines.append("— ничего не найдено. Добавьте RSS-пакет: /addpack @channel ru_world или уточните тему.")
    for index, item in enumerate(check.scores[:5], start=1):
        reasons = ", ".join(item.reasons) if item.reasons else "без доп. причин"
        lines.append(f"{index}. {item.score}/100 — {item.article.title} [{item.article.domain}] — {reasons}")
    await message.answer("\n".join(lines[:20]))


@router.message(Command("sourcepacks"))
async def cmd_sourcepacks(message: Message) -> None:
    registry = SourcePackRegistry()
    packs = registry.list_packs()
    if not packs:
        await message.answer("Пакеты источников не найдены. Проверьте data/source_packs.json")
        return
    lines = ["Готовые пакеты источников:"]
    for pack in packs:
        lines.append(f"• {pack.key} — {pack.title} ({len(pack.sources)} RSS)")
        if pack.description:
            lines.append(f"  {pack.description}")
    lines.append("\nДобавить: /addpack @channel ru_general")
    lines.append("Для международной повестки: /addpack @channel world_general")
    lines.append("Для смеси RU+World можно добавить оба пакета.")
    await message.answer("\n".join(lines))


@router.message(Command("addpack", "pack"))
async def cmd_addpack(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /addpack @channel ru_general\nСписок пакетов: /sourcepacks")
        return
    username = normalize_channel_username(parts[0])
    allowed, reason = channel_allowed_or_text(username)
    if not allowed:
        await message.answer(reason)
        return
    pack_key = parts[1].strip().lower()
    registry = SourcePackRegistry()
    pack = registry.get(pack_key)
    if pack is None:
        await message.answer("Такого пакета нет. Список: /sourcepacks")
        return

    added_sources = 0
    updated_sources = 0
    added_rules = 0
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден. Сначала: /addchannel @channel | тема | стиль")
            return
        new_urls = []
        for item in pack.sources:
            existing = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.url == item.url))).scalar_one_or_none()
            if not existing:
                new_urls.append(item.url)
        try:
            await ensure_source_limit(session, channel, deps().settings, adding=len(new_urls))
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        for item in pack.sources:
            existing = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.url == item.url))).scalar_one_or_none()
            if existing:
                existing.title = item.title
                existing.priority = max(existing.priority or 0, item.priority)
                existing.enabled = True
                updated_sources += 1
            else:
                session.add(Source(channel_id=channel.id, url=item.url, title=item.title, priority=item.priority))
                added_sources += 1
            for rule_type in item.rules:
                if rule_type not in {"trusted", "priority", "suspicious", "block"}:
                    continue
                exists_rule = (await session.execute(
                    select(SourceRule).where(
                        SourceRule.channel_id == channel.id,
                        SourceRule.domain == item.domain,
                        SourceRule.rule_type == rule_type,
                    )
                )).scalar_one_or_none()
                if not exists_rule:
                    session.add(SourceRule(channel_id=channel.id, domain=item.domain, rule_type=rule_type, note=f"source pack: {pack.key}"))
                    added_rules += 1

    await message.answer(
        f"Пакет {pack.key} добавлен для {username}.\n"
        f"Источники: +{added_sources}, обновлено {updated_sources}.\n"
        f"Правила доменов: +{added_rules}.\n"
        "Теперь можно: /news " + username + " [тема]"
    )


@router.message(Command("style"))
async def cmd_style(message: Message) -> None:
    args = command_args(message)
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 2:
        await message.answer("Формат: /style @channel | тон | эмодзи | длина | запрещено | CTA | форматирование")
        return
    username = normalize_channel_username(parts[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        profile.update({
            "tone": parts[1] if len(parts) > 1 else "",
            "emoji": parts[2] if len(parts) > 2 else "",
            "length": parts[3] if len(parts) > 3 else "",
            "forbidden": parts[4] if len(parts) > 4 else "",
            "cta": parts[5] if len(parts) > 5 else "",
            "formatting": parts[6] if len(parts) > 6 else profile.get("formatting", ""),
        })
        channel.style_profile_json = json.dumps(profile, ensure_ascii=False)
    await message.answer(f"Профиль стиля для {username} обновлён. Теперь ИИ будет меньше творить отсебятину, изменения применены..")


@router.message(Command("preset", "пресет"))
async def cmd_preset(message: Message) -> None:
    args = command_args(message)
    if not args or args.strip().lower() in {"list", "список"}:
        await message.answer(
            "Доступные пресеты стиля:\n" + "\n".join(list_preset_lines()) +
            "\n\nПрименить: /preset @channel meme_news\n" +
            "Для живого стиля как на скрине: /preset @channel meme_news"
        )
        return
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /preset @channel classic_news|meme_news|tech_ai|business|expert_blog")
        return
    if message.from_user is None:
        return
    username = normalize_channel_username(parts[0])
    preset_key = parts[1].strip().lower()
    preset = get_preset(preset_key)
    if not preset:
        await message.answer("Такого пресета нет. Список: /preset list")
        return
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await message.answer(error)
            return
        applied = apply_preset_to_channel(channel, preset.key, replace_prompt=False)
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="style_preset_applied", details=f"{username}: {applied.key}"))
    await message.answer(
        f"✅ Пресет применён для {username}: {preset.title}\n\n"
        f"{preset.description}\n\n"
        "Главный промпт канала не затирается, если он уже был задан. Формат и стиль применятся к новым черновикам."
    )


@router.message(Command("format", "fmt"))
async def cmd_format(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Формат: /format @channel html|markdown_v2|plain\n\n"
            "Рекомендую html: он красивый и стабильнее MarkdownV2. "
            "plain — если хочешь вообще без форматирования."
        )
        return
    if message.from_user is None:
        return
    username = normalize_channel_username(parts[0])
    mode = normalize_format_mode(parts[1])
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await message.answer(error)
            return
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        profile["format_mode"] = mode
        # Give the AI a clear style hint too.
        profile.setdefault("formatting", "аккуратное Telegram-оформление: заголовок жирным, короткие абзацы, без визуального шума")
        channel.style_profile_json = json.dumps(profile, ensure_ascii=False)
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="format_mode_updated", details=f"{username}: {mode}"))
    await message.answer(f"✅ Форматирование для {username}: {mode}. Теперь посты будут готовиться под этот режим.")


@router.message(Command("prompt", "edprompt"))
async def cmd_prompt(message: Message) -> None:
    args = command_args(message)
    parts = [p.strip() for p in args.split("|", maxsplit=1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await message.answer(
            "Формат: /prompt @channel | главный промпт канала\n\n"
            "Пример:\n"
            "/prompt @client_news | Ты редактор канала Client News. Пиши про новости отрасли, аналитику, полезные материалы и обновления проекта. Без инфоцыганщины, без фейков, стиль серьёзный и понятный."
        )
        return
    if message.from_user is None:
        return
    username = normalize_channel_username(parts[0])
    prompt_text = parts[1].strip()
    max_chars = deps().settings.editorial_prompt_max_chars
    if len(prompt_text) > max_chars:
        await message.answer(f"Промпт слишком длинный: {len(prompt_text)} символов. Лимит: {max_chars}.")
        return
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await message.answer(error)
            return
        channel.editorial_prompt = prompt_text
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="editorial_prompt_updated", details=f"{username}: {prompt_text[:700]}"))
    await message.answer(
        f"✅ Главный промпт для {username} сохранён.\n\n"
        "Теперь можно делать редакционные посты:\n"
        f"/promptpost {username} | тема поста\n\n"
        "Публикация всё равно пойдёт через черновик и двойное подтверждение. Публикация выполняется только после подтверждения и проверки прав."
    )


@router.message(Command("promptview", "pv"))
async def cmd_prompt_view(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /promptview @channel")
        return
    if message.from_user is None:
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if not channel:
            await message.answer("Канал не найден или не принадлежит текущему администратору.")
            return
        text = channel.editorial_prompt or "Редакционная политика ещё не задана. Используйте: /prompt @channel | текст"
    await message.answer(f"Промпт {username}:\n\n{text}")


@router.message(Command("promptclear"))
async def cmd_prompt_clear(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /promptclear @channel")
        return
    if message.from_user is None:
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await message.answer(error)
            return
        channel.editorial_prompt = ""
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="editorial_prompt_cleared", details=username))
    await message.answer(f"Промпт для {username} очищен.")


@router.message(Command("promptpost", "postto", "ppost"))
async def cmd_prompt_post(message: Message) -> None:
    args = command_args(message)
    parts = [p.strip() for p in args.split("|", maxsplit=1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await message.answer(
            "Формат: /promptpost @ch1,@ch2 | тема/задача\n\n"
            "Пример:\n"
            "/promptpost @client_news | Почему редакции нужна единая контент-система, а не хаотичный постинг"
        )
        return
    if message.from_user is None:
        return
    usernames = parse_channel_list(parts[0])
    task = parts[1].strip()
    if not usernames:
        await message.answer("Не увидел каналы. Укажи @channel или список @ch1,@ch2")
        return
    max_channels = deps().settings.prompt_post_max_channels
    if max_channels > 0 and len(usernames) > max_channels and not is_system_admin(deps().settings, message.from_user.id):
        await message.answer(f"За раз можно максимум {max_channels} каналов для promptpost. Так меньше риск перепутать сетку и слить AI-бюджет.")
        return
    try:
        ensure_network_size(message.from_user.id, usernames, deps().settings)
    except PublicSafetyError as exc:
        await message.answer(str(exc))
        return

    await message.answer("Проверяю права на каждый канал и готовлю черновики по их промптам.")
    previews = []
    async with session_scope() as session:
        channels: list[Channel] = []
        for username in usernames:
            channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
            if not channel:
                await message.answer(error)
                return
            channels.append(channel)
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings, adding=len(channels))
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        for channel in channels:
            text = await deps().writer.write_prompt_post(channel, task, owner_telegram_id=message.from_user.id)
            draft = Draft(
                channel_id=channel.id,
                owner_telegram_id=message.from_user.id,
                kind="prompt",
                text=text,
                confidence="manual",
                risk_notes="Редакционный пост создан по главному промпту канала. Для новостей и фактов используй /news с источниками.",
            )
            session.add(draft)
            await session.flush()
            session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="prompt_draft_created", details=f"draft={draft.id}; channel={channel.username}; task={task[:500]}"))
            previews.append((draft.id, draft.text, None, None, None, 0, channel.username))
    for preview in previews:
        await send_draft_preview(message, *preview)


@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /rules @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        rules = (await session.execute(select(SourceRule).where(SourceRule.channel_id == channel.id).order_by(SourceRule.rule_type, SourceRule.domain))).scalars().all()
    if not rules:
        await message.answer("Правил источников пока нет. Примеры: /trust @channel ign.com или /block @channel badsite.com")
        return
    await message.answer("Правила источников:\n" + "\n".join(f"#{r.id} {r.rule_type}: {r.domain}" for r in rules))


async def _add_rule(message: Message, rule_type: str) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(f"Формат: /{rule_type} @channel domain.com")
        return
    username = normalize_channel_username(parts[0])
    domain = normalize_domain(parts[1])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        session.add(SourceRule(channel_id=channel.id, domain=domain, rule_type=rule_type))
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            await message.answer("Такое правило уже есть.")
            return
    await message.answer(f"Правило добавлено: {rule_type} {domain} для {username}")


@router.message(Command("block"))
async def cmd_block(message: Message) -> None:
    await _add_rule(message, "block")


@router.message(Command("trust"))
async def cmd_trust(message: Message) -> None:
    await _add_rule(message, "trusted")


@router.message(Command("allow"))
async def cmd_allow(message: Message) -> None:
    await _add_rule(message, "trusted")


@router.message(Command("priority"))
async def cmd_priority(message: Message) -> None:
    await _add_rule(message, "priority")


@router.message(Command("suspicious"))
async def cmd_suspicious(message: Message) -> None:
    await _add_rule(message, "suspicious")


@router.message(Command("unrule"))
async def cmd_unrule(message: Message) -> None:
    args = command_args(message)
    try:
        rule_id = int(args.strip())
    except ValueError:
        await message.answer("Формат: /unrule <id>")
        return
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        rule = (await session.execute(select(SourceRule).join(Channel).where(SourceRule.id == rule_id, Channel.owner_id == user.id))).scalar_one_or_none()
        if not rule:
            await message.answer("Правило не найдено или оно не твоё.")
            return
        await session.delete(rule)
    await message.answer(f"Правило #{rule_id} удалено.")


@router.message(Command("media"))
async def cmd_media(message: Message) -> None:
    args = command_args(message)
    parts = args.split()
    if len(parts) != 2 or parts[1] not in {"on", "off"}:
        await message.answer("Формат: /media @channel on|off")
        return
    username = normalize_channel_username(parts[0])
    enabled = parts[1] == "on"
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        channel.media_enabled = enabled
    await message.answer(f"Медиа для {username}: {'включено' if enabled else 'выключено'}")


@router.message(Command("updatesmode"))
async def cmd_updates_mode(message: Message) -> None:
    args = command_args(message)
    parts = args.split()
    if len(parts) != 2 or parts[1] not in {"on", "off"}:
        await message.answer("Формат: /updatesmode @channel on|off")
        return
    username = normalize_channel_username(parts[0])
    enabled = parts[1] == "on"
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        channel.auto_reply_updates = enabled
    await message.answer(f"Ответы-обновления для {username}: {'включены' if enabled else 'выключены'}")


@router.message(Command("news", "gen", "n", "story"))
async def cmd_news(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if not parts:
        await message.answer("Формат: /news @channel [тема]\nОдна команда = одна новость. Для подборки используй /digest @channel тема")
        return
    username = normalize_channel_username(parts[0])
    query = parts[1].strip() if len(parts) > 1 else ""
    await message.answer("Ищу источники, выбираю одну лучшую новость, проверяю дубли и готовлю пост.")
    async with session_scope() as session:
        if message.from_user is None:
            return
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await message.answer(error)
            return
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings)
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        check = await _collect_checked(session, channel, query=query, min_sources=1)
        if not check.accepted:
            await message.answer("Не нашёл свежих источников. Пост не создан — лучше не публиковать непроверенную информацию..")
            return
        fresh = await _filter_new_topics(session, channel, check.accepted)
        if not fresh:
            await message.answer("Похожая новость уже публиковалась. Сработала защита от повторной публикации..")
            return
        selected = fresh[:1]
        text = await deps().writer.write_news_post(channel, selected, check.confidence, check.risk_notes, check.score_payload(), owner_telegram_id=message.from_user.id, post_mode="story")
        draft = _draft_from_articles(channel, message.from_user.id, text, selected, check.confidence, check.risk_notes, quality_score=check.top_score, score_details=check.score_payload())
        session.add(draft)
        await session.flush()
        preview = (draft.id, draft.text, draft.media_url, draft.media_type, draft.reply_to_message_id, draft.quality_score, channel.username)
    await send_draft_preview(message, *preview)


@router.message(Command("digest", "дайджест"))
async def cmd_digest(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if not parts:
        await message.answer("Формат: /digest @channel [тема]\nПример: /digest @client_news технологии и медиа")
        return
    username = normalize_channel_username(parts[0])
    query = parts[1].strip() if len(parts) > 1 else ""
    await message.answer("Собираю дайджест: несколько новостей, но с явными блоками и источниками.")
    async with session_scope() as session:
        if message.from_user is None:
            return
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await message.answer(error)
            return
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings)
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        check = await _collect_checked(session, channel, query=query, min_sources=1)
        if not check.accepted:
            await message.answer("Не нашёл свежих источников для дайджеста.")
            return
        fresh = await _filter_new_topics(session, channel, check.accepted)
        if len(fresh) < 2:
            await message.answer("Для дайджеста мало разных свежих новостей. Используйте /news для одной новости.")
            return
        selected = fresh[:5]
        text = await deps().writer.write_news_post(channel, selected, check.confidence, check.risk_notes, check.score_payload(), owner_telegram_id=message.from_user.id, post_mode="digest")
        draft = _draft_from_articles(channel, message.from_user.id, text, selected, check.confidence, check.risk_notes, kind="digest", quality_score=check.top_score, score_details=check.score_payload())
        session.add(draft)
        await session.flush()
        preview = (draft.id, draft.text, draft.media_url, draft.media_type, draft.reply_to_message_id, draft.quality_score, channel.username)
    await send_draft_preview(message, *preview)

@router.message(Command("updates"))
async def cmd_updates(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if not parts:
        await message.answer("Формат: /updates @channel [тема]")
        return
    username = normalize_channel_username(parts[0])
    query = parts[1].strip() if len(parts) > 1 else ""
    await message.answer("Проверяю, обновились ли уже опубликованные новости.")
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден. Добавьте его через /addchannel")
            return
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings)
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        check = await _collect_checked(session, channel, query=query, min_sources=1)
        if not check.accepted:
            await message.answer("Свежих источников для проверки обновлений не нашёл.")
            return
        candidate = await _find_update_candidate(session, channel, check.accepted)
        if not candidate:
            await message.answer("Обновлений к уже опубликованным новостям не нашёл.")
            return
        article, previous = candidate
        text = await deps().writer.write_update_post(channel, article, previous.article_title, check.confidence, check.risk_notes, owner_telegram_id=message.from_user.id if message.from_user else 0)
        draft = _draft_from_articles(channel, message.from_user.id, text, [article], check.confidence, check.risk_notes, kind="update", reply_to_message_id=previous.message_id, quality_score=check.top_score, score_details=check.score_payload())
        session.add(draft)
        await session.flush()
        preview = (draft.id, draft.text, draft.media_url, draft.media_type, draft.reply_to_message_id, draft.quality_score)
    await send_draft_preview(message, *preview)


@router.message(Command("networkpost", "net"))
async def cmd_network_post(message: Message) -> None:
    args = command_args(message)
    parts = [p.strip() for p in args.split("|", maxsplit=1)]
    if len(parts) != 2:
        await message.answer("Формат: /networkpost @ch1,@ch2,@ch3 | тема")
        return
    usernames = [normalize_channel_username(x) for x in parts[0].split(",") if x.strip()]
    query = parts[1]
    if message.from_user is None:
        return
    try:
        ensure_network_size(message.from_user.id, usernames, deps().settings)
    except PublicSafetyError as exc:
        await message.answer(str(exc))
        return
    if len(usernames) < 2:
        await message.answer("Нужно минимум 2 канала, для сетевого постинга нужно выбрать минимум два канала.")
        return
    await message.answer("Делаю один инфоповод в разных стилях для сетки.")
    previews = []
    async with session_scope() as session:
        channels = []
        for username in usernames:
            ch, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
            if not ch:
                await message.answer(error)
                return
            channels.append(ch)
        if len(channels) < 2:
            await message.answer("Нашёл меньше двух проверенных подключённых каналов. Проверь /channels и /check @channel")
            return
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings, adding=len(channels))
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        check = await _collect_checked(session, channels[0], query=query)
        if not check.accepted:
            await message.answer("Не нашёл источников для сетевого поста.")
            return
        base = await deps().writer.write_news_post(channels[0], check.accepted, check.confidence, check.risk_notes, check.score_payload(), owner_telegram_id=message.from_user.id if message.from_user else 0)
        for ch in channels:
            text = base if ch.id == channels[0].id else await deps().writer.write_network_variant(ch, base, check.accepted, owner_telegram_id=message.from_user.id if message.from_user else 0)
            draft = _draft_from_articles(ch, message.from_user.id, text, check.accepted, check.confidence, check.risk_notes, kind="network", quality_score=check.top_score, score_details=check.score_payload())
            session.add(draft)
            await session.flush()
            previews.append((draft.id, draft.text, draft.media_url, draft.media_type, draft.reply_to_message_id, draft.quality_score))
    for preview in previews:
        await send_draft_preview(message, *preview)


@router.message(Command("ad", "promo"))
async def cmd_ad(message: Message) -> None:
    args = command_args(message)
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 2:
        await message.answer("Формат: /ad @channel | что рекламируем | ссылка | заметки")
        return
    username = normalize_channel_username(parts[0])
    offer = parts[1]
    link = parts[2] if len(parts) > 2 else ""
    notes = parts[3] if len(parts) > 3 else ""
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        try:
            await ensure_daily_draft_limit(session, message.from_user.id, deps().settings)
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        text = await deps().writer.write_ad_post(channel, offer, link, notes, owner_telegram_id=message.from_user.id if message.from_user else 0)
        draft = Draft(channel_id=channel.id, owner_telegram_id=message.from_user.id, kind="ad", text=text, confidence="manual", risk_notes="Рекламный пост создан по ТЗ пользователя. Факты из внешних источников не проверялись.")
        session.add(draft)
        await session.flush()
        preview = (draft.id, draft.text, None, None, None, 0)
    await send_draft_preview(message, *preview)


@router.message(Command("ideas", "idea"))
async def cmd_ideas(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /ideas @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        text = await deps().writer.generate_ideas(channel, owner_telegram_id=message.from_user.id if message.from_user else 0)
    for chunk in split_long_message(text):
        await message.answer(chunk)


@router.message(Command("plan", "contentplan"))
async def cmd_plan(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /plan @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        text = await deps().writer.generate_plan(channel, owner_telegram_id=message.from_user.id if message.from_user else 0)
    for chunk in split_long_message(text):
        await message.answer(chunk)


@router.message(Command("mode", "m"))
async def cmd_mode(message: Message) -> None:
    args = command_args(message)
    parts = args.split()
    if len(parts) != 2 or parts[1] not in {"safe", "semi", "auto"}:
        await message.answer("Формат: /mode @channel safe|semi|auto")
        return
    username = normalize_channel_username(parts[0])
    mode = parts[1]
    if mode == "auto" and message.from_user:
        try:
            ensure_public_auto_allowed(message.from_user.id, deps().settings)
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        channel.mode = mode
    await message.answer(f"Режим {username}: {mode}")


@router.message(Command("autopilot", "auto"))
async def cmd_autopilot(message: Message) -> None:
    args = command_args(message)
    parts = args.split()
    if len(parts) != 2 or parts[1] not in {"on", "off"}:
        await message.answer("Формат: /autopilot @channel on|off")
        return
    username = normalize_channel_username(parts[0])
    enabled = parts[1] == "on"
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        if enabled and channel.mode == "auto":
            try:
                ensure_public_auto_allowed(message.from_user.id, deps().settings)
            except PublicSafetyError as exc:
                await message.answer(str(exc))
                return
        mode_note = ""
        if enabled and channel.mode == "safe":
            channel.mode = "semi"
            mode_note = "\nРежим был safe, переключил в semi: автопилот будет готовить черновики без автопубликации."
        channel.autopilot_enabled = enabled
        now = datetime.now(ZoneInfo(deps().settings.app_timezone))
        slots = due_slots_for_channel(channel, now, deps().settings.autopilot_slot_window_minutes)
        due_note = "\nСлот сейчас активен — автопилот попробует отработать в течение минуты." if enabled and slots else ""
    await message.answer(f"Автопилот для {username}: {'включён' if enabled else 'выключен'}{mode_note}{due_note}")


@router.message(Command("times"))
async def cmd_times(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /times @channel 09:00,14:00,19:00")
        return
    username = normalize_channel_username(parts[0])
    raw_times = parts[1].replace(" ", "")
    try:
        for value in raw_times.split(","):
            datetime.strptime(value, "%H:%M")
    except ValueError:
        await message.answer("Время должно быть в формате HH:MM через запятую.")
        return
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        channel.daily_times = raw_times
    await message.answer(f"Расписание для {username}: {raw_times}")




def _next_autopilot_slots(channel: Channel, now: datetime, limit: int = 3) -> list[str]:
    result: list[str] = []
    parsed: list[tuple[int, int, str]] = []
    for raw in (channel.daily_times or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            hour_raw, minute_raw = raw.split(":", 1)
            hour = int(hour_raw)
            minute = int(minute_raw)
        except Exception:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed.append((hour, minute, f"{hour:02d}:{minute:02d}"))
    for day_offset in range(0, 3):
        base = now + timedelta(days=day_offset)
        for hour, minute, label in parsed:
            candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate >= now:
                result.append(candidate.strftime("%d.%m %H:%M"))
        if len(result) >= limit:
            break
    return result[:limit]


@router.message(Command("autostatus", "astatus"))
async def cmd_autostatus(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /autostatus @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        now = datetime.now(ZoneInfo(deps().settings.app_timezone))
        due = due_slots_for_channel(channel, now, deps().settings.autopilot_slot_window_minutes)
        sources_count = len([s for s in channel.sources if s.enabled]) if channel.sources else 0
        next_slots = _next_autopilot_slots(channel, now)
        last_runs = (
            await session.execute(
                select(AutopilotRun)
                .where(AutopilotRun.channel_id == channel.id)
                .order_by(desc(AutopilotRun.created_at))
                .limit(5)
            )
        ).scalars().all()

    lines = [
        "🤖 Статус автопилота",
        f"Канал: {username}",
        f"Сейчас: {now.strftime('%d.%m %H:%M:%S')} {deps().settings.app_timezone}",
        f"Включён: {'да' if channel.autopilot_enabled else 'нет'}",
        f"Режим: {channel.mode}",
        f"Verified: {'да' if channel.is_verified else 'нет'}",
        f"Источники: {sources_count}",
        f"Расписание: {channel.daily_times or 'не задано'}",
        f"Окно допуска: {deps().settings.autopilot_slot_window_minutes} мин.",
        f"Минимальный рейтинг: {deps().settings.autopilot_min_score}/100",
        f"Слот сейчас: {'да' if due else 'нет'}",
        f"Ближайшие слоты: {', '.join(next_slots) if next_slots else 'нет'}",
    ]
    if last_runs:
        lines.append("Последние запуски:")
        for run in last_runs:
            lines.append(f"— {run.slot_key}: {run.status}")
    else:
        lines.append("Последние запуски: нет")
    await message.answer("\n".join(lines))


@router.message(Command("autorun", "runauto"))
async def cmd_autorun(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /autorun @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        if deps().settings.require_channel_verification and not channel.is_verified:
            await message.answer(f"Канал {username} не verified. Сначала /check {username}")
            return
        service = AutopilotService(deps().settings, deps().bot, deps().collector, deps().checker, deps().writer, deps().publisher)
        await message.answer(f"Запускаю автопилот вручную для {username}. Если всё ок, появится черновик или уведомление с причиной пропуска.")
        draft = await service.run_for_channel(session, channel)
        if draft:
            await message.answer(f"Готово: создан черновик #{draft.id}.")
        else:
            await message.answer("Автопилот завершился без черновика. Причину смотри в сообщении выше или проверь /sourcecheck.")

@router.message(Command("schedulepost", "sched"))
async def cmd_schedule_post(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: /schedulepost <draft_id> YYYY-MM-DD HH:MM")
        return
    try:
        draft_id = int(parts[0])
        run_at = datetime.strptime(parts[1] + " " + parts[2], "%Y-%m-%d %H:%M")
    except ValueError:
        await message.answer("Не понял дату. Формат: /schedulepost 12 2026-06-04 19:30")
        return
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        if not draft or not message.from_user or draft.owner_telegram_id != message.from_user.id:
            await message.answer("Черновик не найден или недоступен текущему администратору.")
            return
        channel = await session.get(Channel, draft.channel_id)
        if channel:
            try:
                await ensure_schedule_limit(session, channel, deps().settings)
            except PublicSafetyError as exc:
                await message.answer(str(exc))
                return
        draft.status = "scheduled"
        session.add(ScheduledPost(draft_id=draft.id, channel_id=draft.channel_id, run_at=run_at))
    await message.answer(f"Черновик #{draft_id} запланирован на {run_at:%Y-%m-%d %H:%M}.")


@router.message(Command("draft"))
async def cmd_draft(message: Message) -> None:
    args = command_args(message)
    if not args or not args.split()[0].isdigit():
        await message.answer("Формат: /draft <id>")
        return
    draft_id = int(args.split()[0])
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        if not draft or not message.from_user or draft.owner_telegram_id != message.from_user.id or draft.status != "draft":
            await message.answer("Черновик не найден или уже не активен.")
            return
        channel = await session.get(Channel, draft.channel_id)
        channel_username = channel.username if channel else None
        preview = (draft.id, draft.text, draft.media_url, draft.media_type, draft.reply_to_message_id, draft.quality_score, channel_username)
    await send_draft_preview(message, *preview)


@router.message(Command("publish", "approve"))
async def cmd_publish(message: Message) -> None:
    args = command_args(message)
    if not args or not args.split()[0].isdigit():
        await message.answer("Формат: /publish <draft_id>")
        return
    draft_id = int(args.split()[0])
    if message.from_user is None:
        return
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        if not draft or draft.owner_telegram_id != message.from_user.id or draft.status != "draft":
            await message.answer("Черновик не найден или уже не активен.")
            return
        channel = await session.get(Channel, draft.channel_id)
        if not channel:
            await message.answer("Канал черновика не найден.")
            return
        try:
            user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one_or_none()
            role = user_role(deps().settings, user, message.from_user.id)
            if not role_can(role, "publish"):
                await add_audit_event(session, user_id=message.from_user.id, role=role, channel=channel, action="publish_blocked", payload={"draft_id": draft.id, "source": "command"}, result="blocked", reason="insufficient_role", draft_id=draft.id)
                raise ValueError("Недостаточно прав для публикации")
            ok, reason = await verify_channel_for_user(channel.username, message.from_user.id)
            if not ok:
                await add_audit_event(session, user_id=message.from_user.id, role=role, channel=channel, action="publish_blocked", payload={"draft_id": draft.id, "source": "command"}, result="blocked", reason=reason, draft_id=draft.id)
                raise ValueError(reason)
            await deps().publisher.publish_draft(session, draft)
            await add_audit_event(session, user_id=message.from_user.id, role=role, channel=channel, action="publish", payload={"draft_id": draft.id, "kind": draft.kind, "source": "command"}, result="success", draft_id=draft.id)
        except Exception as exc:
            await message.answer(f"Не удалось опубликовать черновик #{draft_id}: {exc}")
            return
    await message.answer(f"Готово. Черновик #{draft_id} опубликован.")


@router.message(Command("queue", "q"))
async def cmd_queue(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /queue @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        scheduled_rows = (await session.execute(select(ScheduledPost, Draft).join(Draft, Draft.id == ScheduledPost.draft_id).where(ScheduledPost.channel_id == channel.id, ScheduledPost.status == "pending").order_by(ScheduledPost.run_at.asc()).limit(20))).all()
        draft_rows = (await session.execute(select(Draft).where(Draft.channel_id == channel.id, Draft.owner_telegram_id == message.from_user.id, Draft.status == "draft").order_by(desc(Draft.created_at), desc(Draft.id)).limit(10))).scalars().all()
        draft_previews = [(dr.id, dr.kind, dr.quality_score, dr.created_at) for dr in draft_rows]
    if not scheduled_rows and not draft_previews:
        await message.answer("Очередь и активные черновики пустые.")
        return
    lines = [f"Очередь для {username}:"]
    if draft_previews:
        lines.append("\nАктивные черновики:")
        lines += [f"#{draft_id} — {kind} — рейтинг {score}/100 — /draft {draft_id} — /publish {draft_id}" for draft_id, kind, score, created_at in draft_previews]
    if scheduled_rows:
        lines.append("\nЗапланированные публикации:")
        lines += [f"#{sp.id} draft #{dr.id} — {sp.run_at:%Y-%m-%d %H:%M} — {dr.kind}" for sp, dr in scheduled_rows]
    await message.answer("\n".join(lines))
    for draft_id, kind, score, created_at in draft_previews[:5]:
        await message.answer(f"Черновик #{draft_id} — {kind} — рейтинг {score}/100", reply_markup=draft_keyboard(draft_id))


@router.message(Command("calendar", "cal"))
async def cmd_calendar(message: Message) -> None:
    args = command_args(message)
    parts = args.split()
    if not parts:
        await message.answer("Формат: /calendar @channel [days]")
        return
    username = normalize_channel_username(parts[0])
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    until = since + timedelta(days=days + 1)
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        rows = (await session.execute(select(ScheduledPost, Draft).join(Draft, Draft.id == ScheduledPost.draft_id).where(ScheduledPost.channel_id == channel.id, ScheduledPost.run_at >= since, ScheduledPost.run_at <= until).order_by(ScheduledPost.run_at.asc()))).all()
    if not rows:
        await message.answer("В календаре пока пусто.")
        return
    lines = [f"{sp.run_at:%d.%m %H:%M} — {sp.status} — draft #{dr.id} [{dr.kind}]" for sp, dr in rows]
    await message.answer("Календарь:\n" + "\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    args = command_args(message)
    try:
        scheduled_id = int(args.strip())
    except ValueError:
        await message.answer("Формат: /cancel <scheduled_id>")
        return
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        row = (await session.execute(select(ScheduledPost).join(Channel, Channel.id == ScheduledPost.channel_id).where(ScheduledPost.id == scheduled_id, Channel.owner_id == user.id))).scalar_one_or_none()
        if not row:
            await message.answer("Запланированный пост не найден или недоступен текущему администратору.")
            return
        row.status = "cancelled"
    await message.answer(f"Пост в очереди #{scheduled_id} отменён.")


@router.message(Command("review", "checkdraft"))
async def cmd_review_draft(message: Message) -> None:
    args = command_args(message)
    if not args or not args.split()[0].isdigit():
        await message.answer("Формат: /review <draft_id>")
        return
    if message.from_user is None:
        return
    draft_id = int(args.split()[0])
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        if not draft or draft.owner_telegram_id != message.from_user.id:
            await message.answer("Черновик не найден.")
            return
        review = review_post_text(draft.text, require_source=(draft.kind in {"news", "digest", "update"} and public_post_sources_visible()), digest=draft.kind == "digest")
        draft.risk_notes = (draft.risk_notes or "") + "\n" + review.summary()
    await message.answer(review.summary())


@router.message(Command("stats", "stat"))
async def cmd_stats(message: Message) -> None:
    args = command_args(message)
    if not args:
        await message.answer("Формат: /stats @channel")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        drafts = (await session.execute(select(func.count(Draft.id)).where(Draft.channel_id == channel.id))).scalar_one()
        published = (await session.execute(select(func.count(PublishedPost.id)).where(PublishedPost.channel_id == channel.id))).scalar_one()
        scheduled = (await session.execute(select(func.count(ScheduledPost.id)).where(ScheduledPost.channel_id == channel.id, ScheduledPost.status == "pending"))).scalar_one()
        avg_score = (await session.execute(select(func.avg(Draft.quality_score)).where(Draft.channel_id == channel.id, Draft.quality_score > 0))).scalar_one()
    await message.answer(f"Статистика {username}:\nЧерновиков: {drafts}\nОпубликовано: {published}\nВ очереди: {scheduled}\nСредний рейтинг новостей: {round(float(avg_score), 1) if avg_score else 'нет данных'}")


@router.message(Command("partneradd"))
async def cmd_partner_add(message: Message) -> None:
    args = command_args(message)
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 3:
        await message.answer("Формат: /partneradd @my_channel | @partner | условия | YYYY-MM-DD")
        return
    my_channel = normalize_channel_username(parts[0])
    partner = normalize_channel_username(parts[1])
    terms = parts[2]
    due_at = None
    if len(parts) > 3 and parts[3]:
        try:
            due_at = datetime.strptime(parts[3], "%Y-%m-%d")
        except ValueError:
            await message.answer("Дата должна быть YYYY-MM-DD")
            return
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        channel = await find_owned_channel(session, user.telegram_id, my_channel)
        if not channel:
            await message.answer("Твой канал не найден.")
            return
        deal = PartnerDeal(owner_id=user.id, channel_id=channel.id, partner_channel=partner, terms=terms, due_at=due_at)
        session.add(deal)
        await session.flush()
        deal_id = deal.id
    await message.answer(f"Партнёрство #{deal_id} добавлено: {partner}. Партнёрство добавлено в учёт.")


@router.message(Command("partners"))
async def cmd_partners(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        rows = (await session.execute(select(PartnerDeal).where(PartnerDeal.owner_id == user.id, PartnerDeal.status == "active").order_by(PartnerDeal.due_at.asc().nullslast(), PartnerDeal.created_at.desc()))).scalars().all()
    if not rows:
        await message.answer("Активных партнёрств нет.")
        return
    lines = []
    for r in rows:
        due = r.due_at.strftime("%Y-%m-%d") if r.due_at else "без даты"
        lines.append(f"#{r.id} {r.partner_channel} — до {due} — {r.terms}")
    await message.answer("Партнёры/бартеры:\n" + "\n".join(lines))


@router.message(Command("partnerdone"))
async def cmd_partner_done(message: Message) -> None:
    args = command_args(message)
    try:
        deal_id = int(args.strip())
    except ValueError:
        await message.answer("Формат: /partnerdone <id>")
        return
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        deal = (await session.execute(select(PartnerDeal).where(PartnerDeal.id == deal_id, PartnerDeal.owner_id == user.id))).scalar_one_or_none()
        if not deal:
            await message.answer("Партнёрство не найдено.")
            return
        deal.status = "done"
    await message.answer(f"Партнёрство #{deal_id} отмечено выполненным.")




@router.message(Command("roles"))
async def cmd_roles(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        current = await get_or_create_user(session, message)
        current_role = user_role(deps().settings, current, message.from_user.id)
        if current_role != "owner":
            await reply_md(message, "⚠️ **Недостаточно прав**\n\nРоли может смотреть только owner.")
            return
        rows = (await session.execute(select(User).order_by(User.created_at.asc()).limit(50))).scalars().all()
    lines = ["👥 **Доступ к рабочему пространству**", ""]
    for user in rows:
        role = "owner" if user.telegram_id in deps().settings.admin_ids else (user.role or "viewer")
        name = user.full_name or user.username or "без имени"
        lines.append(f"• `{user.telegram_id}` — **{role}** — {name}")
    await reply_md(message, "\n".join(lines))


async def _set_user_role(message: Message, target_role: str) -> None:
    if message.from_user is None:
        return
    args = command_args(message).strip()
    if not args or not args.split()[0].isdigit():
        await reply_md(message, f"Формат: `/{'addoperator' if target_role == 'operator' else 'addviewer' if target_role == 'viewer' else 'removeoperator'} <telegram_id>`")
        return
    target_id = int(args.split()[0])
    async with session_scope() as session:
        current = await get_or_create_user(session, message)
        if user_role(deps().settings, current, message.from_user.id) != "owner":
            await reply_md(message, "⚠️ **Недостаточно прав**\n\nРоли может менять только owner.")
            return
        user = (await session.execute(select(User).where(User.telegram_id == target_id))).scalar_one_or_none()
        if not user:
            user = User(telegram_id=target_id, role=target_role)
            session.add(user)
        else:
            user.role = target_role
        await add_audit_event(session, user_id=message.from_user.id, role="owner", action="role_changed", payload={"target_id": target_id, "role": target_role})
    await reply_md(message, f"✅ Роль пользователя `{target_id}` изменена на **{target_role}**.")


@router.message(Command("addoperator"))
async def cmd_add_operator(message: Message) -> None:
    await _set_user_role(message, "operator")


@router.message(Command("addviewer"))
async def cmd_add_viewer(message: Message) -> None:
    await _set_user_role(message, "viewer")


@router.message(Command("removeoperator", "removeviewer"))
async def cmd_remove_role(message: Message) -> None:
    await _set_user_role(message, "viewer")


@router.message(Command("myrole"))
async def cmd_myrole(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        role = user_role(deps().settings, user, message.from_user.id)
    await reply_md(message, f"Ваша роль: **{role}**")


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    if message.from_user is None:
        return
    args = command_args(message).strip()
    username = normalize_channel_username(args.split()[0]) if args else ""
    async with session_scope() as session:
        total_expr = func.coalesce(func.sum(AIUsageEvent.prompt_tokens_est + AIUsageEvent.completion_tokens_est), 0)
        count_expr = func.count(AIUsageEvent.id)
        daily_tokens = (await session.execute(select(total_expr).where(AIUsageEvent.owner_telegram_id == message.from_user.id, AIUsageEvent.cached.is_(False), AIUsageEvent.created_at >= today_start_utc()))).scalar_one()
        daily_calls = (await session.execute(select(count_expr).where(AIUsageEvent.owner_telegram_id == message.from_user.id, AIUsageEvent.created_at >= today_start_utc()))).scalar_one()
        month_tokens = (await session.execute(select(total_expr).where(AIUsageEvent.owner_telegram_id == message.from_user.id, AIUsageEvent.cached.is_(False), AIUsageEvent.created_at >= month_start_utc()))).scalar_one()
    await reply_md(
        message,
        "📊 **Usage**\n\n"
        f"AI-запросы сегодня: **{int(daily_calls or 0)}** / `{deps().settings.max_ai_requests_per_day}`\n"
        f"Токены сегодня est.: **{int(daily_tokens or 0)}** / `{deps().settings.ai_daily_token_budget}`\n"
        f"Токены за месяц est.: **{int(month_tokens or 0)}** / `{deps().settings.ai_monthly_token_budget}`\n\n"
        "Это оценка для контроля бюджета, не точный счёт провайдера до копейки."
    )


@router.message(Command("limits"))
async def cmd_limits(message: Message) -> None:
    st = deps().settings
    await reply_md(
        message,
        "🧱 **Лимиты проекта**\n\n"
        f"AI-запросов в день: `{st.max_ai_requests_per_day}`\n"
        f"AI токенов в день est.: `{st.ai_daily_token_budget}`\n"
        f"AI токенов в месяц est.: `{st.ai_monthly_token_budget}`\n"
        f"NewsAPI запросов в день: `{st.max_newsapi_requests_per_day}`\n"
        f"Черновиков в день: `{st.public_max_drafts_per_day}`\n"
        f"Дайджестов в день: `{st.max_digests_per_day}`\n"
        f"Каналов в одном запросе: `{st.prompt_post_max_channels}`\n"
        f"Demo mode: **{'on' if st.demo_mode else 'off'}**"
    )


@router.message(Command("audit"))
async def cmd_audit(message: Message) -> None:
    if message.from_user is None:
        return
    args = command_args(message).strip()
    username = normalize_channel_username(args.split()[0]) if args else ""
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        role = user_role(deps().settings, user, message.from_user.id)
        if role not in {"owner", "operator"}:
            await reply_md(message, "⚠️ **Недостаточно прав**\n\nAudit доступен owner/operator.")
            return
        query = select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(20)
        if username:
            query = select(AuditEvent).where(AuditEvent.channel_username == username).order_by(desc(AuditEvent.created_at)).limit(20)
        rows = (await session.execute(query)).scalars().all()
    if not rows:
        await reply_md(message, "📭 **Audit пуст**")
        return
    lines = ["🧾 **Audit log**", ""]
    for e in rows:
        ch = f" {e.channel_username}" if e.channel_username else ""
        lines.append(f"• {e.created_at:%d.%m %H:%M} — `{e.user_id}` — **{e.action_type}**{ch} — {e.result}")
    await reply_md(message, "\n".join(lines))


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    # Alias with a more user-friendly name.
    await cmd_audit(message)


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if message.from_user is None:
        return
    args = command_args(message).strip()
    if not args:
        await reply_md(message, "Формат: `/export @channel`")
        return
    username = normalize_channel_username(args.split()[0])
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id, require_verified=False)
        if not channel:
            await reply_md(message, f"⚠️ **Экспорт недоступен**\n\n{error}")
            return
        sources = (await session.execute(select(Source).where(Source.channel_id == channel.id).order_by(Source.id))).scalars().all()
        rules = (await session.execute(select(SourceRule).where(SourceRule.channel_id == channel.id).order_by(SourceRule.id))).scalars().all()
        data = export_channel_config(channel, sources, rules)
    for chunk in split_long_message("```json\n" + data + "\n```"):
        await reply_md(message, chunk)


@router.message(Command("day", "contentday", "dayplan"))
async def cmd_day_plan(message: Message) -> None:
    if message.from_user is None:
        return
    args = command_args(message).strip()
    if not args:
        await reply_md(message, "Формат: `/day @channel [preset]`\nПример: `/day @client_news classic_news`")
        return
    parts = args.split(maxsplit=1)
    username = normalize_channel_username(parts[0])
    preset = parts[1].strip() if len(parts) > 1 else ""
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id, require_verified=False)
        if not channel:
            await reply_md(message, f"⚠️ **План недоступен**\n\n{error}")
            return
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        preset_key = preset or str(profile.get("preset") or profile.get("post_style") or "classic_news")
        plan = build_day_plan(channel.username, preset_key)
        await add_audit_event(session, user_id=message.from_user.id, role="owner" if message.from_user.id in deps().settings.admin_ids else "operator", channel=channel, action="content_day_plan", payload={"preset": preset_key})
    await reply_md(message, "🗓 **План контента**\n\n" + plan)


@router.message(Command("launchcheck", "qa", "checklist"))
async def cmd_launch_check(message: Message) -> None:
    await reply_md(message, launch_checklist_text())


@router.message(Command("demo"))
async def cmd_demo_script(message: Message) -> None:
    await reply_md(message, demo_script_text())


@router.message(Command("offer"))
async def cmd_offer(message: Message) -> None:
    await reply_md(message, commercial_offer_text())


@router.message(Command("pricing", "price"))
async def cmd_pricing(message: Message) -> None:
    await reply_md(message, pricing_text())


@router.message(Command("setup", "wizard", "onboarding"))
async def cmd_setup_master(message: Message) -> None:
    if message.from_user is None:
        await reply_md(message, setup_master_text())
        return
    async with session_scope() as session:
        channels = [ch.username for ch in await load_user_channels(session, message.from_user.id)]
    await reply_md(message, setup_master_text(channels))


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(
        "Главное меню ChannelPilot Pro. Выберите действие кнопкой или напишите задачу обычным языком.",
        reply_markup=menu_keyboard(),
    )


@router.message(Command("budget"))
async def cmd_budget(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        total_expr = func.coalesce(func.sum(AIUsageEvent.prompt_tokens_est + AIUsageEvent.completion_tokens_est), 0)
        today_used = (await session.execute(
            select(total_expr).where(
                AIUsageEvent.owner_telegram_id == message.from_user.id,
                AIUsageEvent.cached.is_(False),
                AIUsageEvent.created_at >= today_start_utc(),
            )
        )).scalar_one()
        month_used = (await session.execute(
            select(total_expr).where(
                AIUsageEvent.owner_telegram_id == message.from_user.id,
                AIUsageEvent.cached.is_(False),
                AIUsageEvent.created_at >= month_start_utc(),
            )
        )).scalar_one()
        cache_hits = (await session.execute(
            select(func.count(AIUsageEvent.id)).where(
                AIUsageEvent.owner_telegram_id == message.from_user.id,
                AIUsageEvent.cached.is_(True),
                AIUsageEvent.created_at >= month_start_utc(),
            )
        )).scalar_one()
    settings = deps().settings
    await message.answer(
        "AI-бюджет:\n"
        f"Сегодня: {int(today_used or 0)} / {settings.ai_daily_token_budget} токенов est.\n"
        f"Месяц: {int(month_used or 0)} / {settings.ai_monthly_token_budget} токенов est.\n"
        f"Cache hits за месяц: {int(cache_hits or 0)}\n"
        f"Режим экономии по умолчанию: {settings.cost_saver_mode}\n"
        "\nЭто оценка по символам, не биллинг провайдера до копейки. Но для контроля расходов хватает."
    )


@router.message(Command("costmode"))
async def cmd_costmode(message: Message) -> None:
    args = command_args(message)
    parts = args.split()
    if len(parts) != 2 or parts[1].lower() not in {"cheap", "balanced", "quality"}:
        await message.answer("Формат: /costmode @channel cheap|balanced|quality\ncheap — минимум токенов, balanced — норм баланс, quality — дороже, но подробнее.")
        return
    username = normalize_channel_username(parts[0])
    mode = parts[1].lower()
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username) if message.from_user else None
        if not channel:
            await message.answer("Канал не найден.")
            return
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        profile["cost_mode"] = mode
        channel.style_profile_json = json.dumps(profile, ensure_ascii=False)
    await message.answer(f"Режим AI-расходов для {username}: {mode}")


@router.message(Command("doctor"))
async def cmd_doctor(message: Message) -> None:
    settings = deps().settings
    lines = ["Диагностика ChannelPilot Pro:"]
    lines.append(f"PostgreSQL: ok")
    lines.append(f"AI key: {'ok' if settings.ai_api_key else 'нет, будет fallback'}")
    lines.append(f"NewsAPI: {'ok' if settings.newsapi_key else 'нет'}")
    lines.append(f"Brave Search: {'ok' if settings.brave_search_api_key else 'нет'}")
    lines.append(f"Edition: {settings.project_edition}")
    lines.append(f"Project: {settings.project_name}")
    lines.append(f"Access mode: {settings.bot_access_mode}")
    lines.append(f"Allowed channels: {', '.join(settings.allowed_channels) if settings.allowed_channels else 'не задано — разрешён любой проверенный канал админа'}")
    lines.append(f"ADMIN_IDS: {'заданы' if settings.admin_ids else 'НЕ ЗАДАНЫ — защищённые команды будут закрыты'}")
    lines.append(f"Require channel verification: {settings.require_channel_verification}")
    lines.append(f"Public terms: {settings.public_require_terms_acceptance}")
    lines.append(f"User must be channel admin: {settings.public_require_user_channel_admin}")
    lines.append(f"Public auto publish allowed: {settings.public_allow_auto_publish}")
    lines.append(f"Cost saver: {'on' if settings.cost_saver_enabled else 'off'} / {settings.cost_saver_mode}")
    if message.from_user:
        async with session_scope() as session:
            channels = (await session.execute(
                select(Channel).join(User).where(User.telegram_id == message.from_user.id).order_by(Channel.created_at.desc())
            )).scalars().all()
        lines.append(f"Каналов: {len(channels)}")
        for ch in channels[:8]:
            verified, info = await deps().publisher.verify_channel_permissions(ch.username)
            lines.append(f"• {ch.username}: {'ok' if verified else 'problem'} — {info}")
    await message.answer("\n".join(lines))


@router.message(Command("quick", "setup"))
async def cmd_quick_setup(message: Message) -> None:
    args = command_args(message)
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /quick @channel ru_world\nПакеты смотрите: /sourcepacks")
        return
    username = normalize_channel_username(parts[0])
    allowed, reason = channel_allowed_or_text(username)
    if not allowed:
        await message.answer(reason)
        return
    pack_key = parts[1].strip().lower()
    registry = SourcePackRegistry()
    pack = registry.get(pack_key)
    if pack is None:
        await message.answer("Пакет не найден. Список: /sourcepacks")
        return
    topic = pack.title + ". " + (pack.description or "новости и полезный контент")
    style = "коротко, понятно, на русском, без кликбейта, с источниками"
    if message.from_user is None:
        return
    verified, verify_message = await verify_channel_for_user(username, message.from_user.id)
    if not verified and deps().settings.public_require_user_channel_admin and not is_system_admin(deps().settings, message.from_user.id):
        await message.answer("Быстрая настройка остановлена: " + verify_message + "\n\nДобавьте бота администратором с правом публикации и убедитесь, что пользователь является администратором канала.")
        return
    added_sources = 0
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        try:
            await ensure_channel_claimable(session, user, username)
            channel = (await session.execute(select(Channel).where(Channel.owner_id == user.id, Channel.username == username))).scalar_one_or_none()
            if not channel:
                await ensure_channel_limit(session, user, deps().settings)
                channel = Channel(owner_id=user.id, username=username, title=verify_message if verified else None, topic=topic, style=style, min_sources=deps().settings.default_min_sources, is_verified=verified, media_enabled=deps().settings.media_enabled_by_default)
                session.add(channel)
                await session.flush()
            else:
                channel.topic = topic
                channel.style = style
                channel.is_verified = verified
                channel.title = verify_message if verified else channel.title
            new_urls = []
            for item in pack.sources:
                existing = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.url == item.url))).scalar_one_or_none()
                if not existing:
                    new_urls.append(item.url)
            await ensure_source_limit(session, channel, deps().settings, adding=len(new_urls))
        except PublicSafetyError as exc:
            await message.answer(str(exc))
            return
        for item in pack.sources:
            existing = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.url == item.url))).scalar_one_or_none()
            if not existing:
                session.add(Source(channel_id=channel.id, url=item.url, title=item.title, priority=item.priority))
                added_sources += 1
            for rule_type in item.rules:
                if rule_type not in {"trusted", "priority", "suspicious", "block"}:
                    continue
                exists_rule = (await session.execute(select(SourceRule).where(SourceRule.channel_id == channel.id, SourceRule.domain == item.domain, SourceRule.rule_type == rule_type))).scalar_one_or_none()
                if not exists_rule:
                    session.add(SourceRule(channel_id=channel.id, domain=item.domain, rule_type=rule_type, note=f"auto from pack {pack.key}"))
    await message.answer(
        f"Быстрая настройка готова для {username}.\n"
        f"Пакет: {pack.key}\nДобавлено RSS: {added_sources}\n"
        f"Права канала: {'ok' if verified else 'проблема — ' + verify_message}\n\n"
        f"Теперь попробуй: /news {username}\nЭкономный режим: /costmode {username} cheap"
    )


@router.message(Command("adminusers"))
async def cmd_admin_users(message: Message) -> None:
    if not message.from_user or not is_system_admin(deps().settings, message.from_user.id):
        return
    async with session_scope() as session:
        rows = (await session.execute(select(User).order_by(User.created_at.desc()).limit(30))).scalars().all()
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    lines = []
    for u in rows:
        state = "blocked" if u.is_blocked else "ok"
        accepted = "accepted" if u.accepted_terms_at else "no_terms"
        lines.append(f"{u.telegram_id} @{u.username or '-'} — {state}, {accepted}, {u.full_name or '-'}")
    await message.answer("Последние пользователи:\n" + "\n".join(lines))


@router.message(Command("adminchannels"))
async def cmd_admin_channels(message: Message) -> None:
    if not message.from_user or not is_system_admin(deps().settings, message.from_user.id):
        return
    async with session_scope() as session:
        rows = (await session.execute(select(Channel).options(selectinload(Channel.owner)).order_by(Channel.created_at.desc()).limit(40))).scalars().all()
    if not rows:
        await message.answer("Каналов пока нет.")
        return
    lines = []
    for ch in rows:
        lines.append(f"{ch.username} — owner={ch.owner.telegram_id if ch.owner else '-'} — verified={'yes' if ch.is_verified else 'no'} — mode={ch.mode}")
    await message.answer("Каналы:\n" + "\n".join(lines))


@router.message(Command("ban"))
async def cmd_ban(message: Message) -> None:
    if not message.from_user or not is_system_admin(deps().settings, message.from_user.id):
        return
    args = command_args(message)
    parts = args.split(maxsplit=1)
    try:
        telegram_id = int(parts[0])
    except Exception:
        await message.answer("Формат: /ban <telegram_id> [причина]")
        return
    reason = parts[1] if len(parts) > 1 else "blocked by admin"
    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.flush()
        user.is_blocked = True
        user.blocked_reason = reason
        session.add(SecurityEvent(owner_telegram_id=telegram_id, event_type="admin_ban", details=reason[:1000]))
    await message.answer(f"Пользователь {telegram_id} заблокирован.")


@router.message(Command("unban"))
async def cmd_unban(message: Message) -> None:
    if not message.from_user or not is_system_admin(deps().settings, message.from_user.id):
        return
    try:
        telegram_id = int(command_args(message).strip())
    except Exception:
        await message.answer("Формат: /unban <telegram_id>")
        return
    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
        if not user:
            await message.answer("Пользователь не найден.")
            return
        user.is_blocked = False
        user.blocked_reason = ""
        session.add(SecurityEvent(owner_telegram_id=telegram_id, event_type="admin_unban", details=""))
    await message.answer(f"Пользователь {telegram_id} разблокирован.")


@router.message(Command("securitylog"))
async def cmd_security_log(message: Message) -> None:
    if not message.from_user or not is_system_admin(deps().settings, message.from_user.id):
        return
    async with session_scope() as session:
        rows = (await session.execute(select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(30))).scalars().all()
    if not rows:
        await message.answer("Событий безопасности пока нет.")
        return
    lines = [f"{e.created_at:%d.%m %H:%M} — {e.owner_telegram_id} — {e.event_type} — {e.details[:80]}" for e in rows]
    await message.answer("Журнал безопасности:\n" + "\n".join(lines))



@router.message(F.text)
async def natural_language_control(message: Message) -> None:
    """v12 Natural AI Control: normal messages become safe internal actions.

    Slash commands stay as emergency/technical mode. This handler only works in
    private chat and never publishes directly; it creates drafts or updates safe
    settings through the same channel guards as commands.
    """
    text = (message.text or "").strip()
    if not text or text.startswith("/") or message.from_user is None:
        return
    if getattr(message.chat, "type", "private") != "private":
        return
    if not deps().settings.natural_control_enabled:
        await reply_md(message, "⚙️ **Natural Control выключен**\n\nВключи `NATURAL_CONTROL_ENABLED=true`, если хочешь управлять ботом обычным текстом.")
        return

    async with session_scope() as session:
        user_channels = await load_user_channels(session, message.from_user.id)
    router_service = NaturalLanguageRouter(deps().writer, use_ai=deps().settings.natural_control_use_ai)
    route = await router_service.route(text, user_channels, owner_telegram_id=message.from_user.id)
    await _execute_natural_route(message, route, user_channels)


async def _execute_natural_route(message: Message, route: RouteResult, user_channels: list[Channel]) -> None:
    if message.from_user is None:
        return
    action = route.action

    if action == "help":
        await reply_md(
            message,
            "**Natural Control включён**\n\n"
            "Можно писать обычным языком:\n"
            "• `Сделай пост для @client_news про ключевые события недели`\n"
            "• `Найди актуальную новость для @industry_news про новый продукт`\n"
            "• `Сделай дайджест для @world_digest по международным новостям`\n"
            "• `Поставь markdown для @client_news`\n"
            "• `Покажи очередь @client_news`\n\n"
            "Команды доступны как резервный режим: `/help`."
        )
        return

    if action == "show_channels":
        await _natural_show_channels(message)
        return

    if route.needs_channel and not route.channels:
        available = "\n".join(f"• `{ch.username}` — {ch.title or ch.topic[:40] or 'канал'}" for ch in user_channels) or "пока нет подключённых каналов"
        await reply_md(
            message,
            "⚠️ **Не понял, какой канал использовать**\n\n"
            f"Доступные каналы:\n{available}\n\n"
            "Напиши задачу ещё раз с `@channel`, например:\n"
            "`Сделай пост для @client_news про новости отрасли`."
        )
        return

    if action == "unknown":
        await reply_md(
            message,
            "⚠️ **Не удалось определить задачу**\n\n"
            f"{route.clarification or 'Укажите, что нужно сделать и для какого канала.'}\n\n"
            "Пример: `Сделай дайджест для @client_news про технологии и медиа`."
        )
        return

    if action == "connect_channel":
        await _natural_connect_channel(message, route.channels[0], route.topic)
        return
    if action == "check_channel":
        await _natural_check_channel(message, route.channels[0])
        return
    if action == "set_prompt":
        await _natural_set_prompt(message, route.channels[0], route.prompt_text or route.topic)
        return
    if action == "show_prompt":
        await _natural_show_prompt(message, route.channels[0])
        return
    if action == "create_news":
        await create_news_or_digest_draft(message, route.channels[0], route.topic or text, digest_mode=False)
        return
    if action == "create_digest":
        await create_news_or_digest_draft(message, route.channels[0], route.topic or text, digest_mode=True)
        return
    if action == "create_prompt_post":
        await create_prompt_drafts_for_channels(message, route.channels, route.topic or text)
        return
    if action == "set_format":
        await _natural_set_format(message, route.channels[0], route.format_mode or "markdown_v2")
        return
    if action == "set_preset":
        await _natural_set_preset(message, route.channels[0], route.preset or route.topic or "")
        return
    if action == "add_source_pack":
        await _natural_add_source_pack(message, route.channels[0], route.source_pack)
        return
    if action == "add_source":
        await _natural_add_source(message, route.channels[0], route.source_url)
        return
    if action == "show_queue":
        await _natural_show_queue(message, route.channels[0])
        return
    if action == "show_calendar":
        await _natural_show_calendar(message, route.channels[0])
        return
    if action == "show_usage":
        await _natural_show_usage(message)
        return
    if action == "show_limits":
        await _natural_show_limits(message)
        return
    if action == "show_audit":
        await _natural_show_audit(message, route.channels[0] if route.channels else "")
        return
    if action == "content_day":
        await _natural_content_day(message, route.channels[0])
        return
    if action == "export_channel":
        await _natural_export_channel(message, route.channels[0])
        return

    await reply_md(message, "⚠️ **Действие пока недоступно через диалог**\n\nИспользуйте резервные команды из `/help`.")


async def _natural_show_channels(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        rows = await load_user_channels(session, message.from_user.id)
    if not rows:
        await reply_md(message, "📭 **Каналов пока нет**\n\nДобавьте бота администратором в канал и напишите: `Подключи канал @channel`.")
        return
    lines = []
    for ch in rows:
        lines.append(f"• `{ch.username}` — verified: **{'да' if ch.is_verified else 'нет'}**, prompt: **{'да' if (ch.editorial_prompt or '').strip() else 'нет'}**")
    await reply_md(message, "📚 **Подключённые каналы**\n\n" + "\n".join(lines))


async def _natural_connect_channel(message: Message, username: str, topic: str = "") -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    allowed, reason = channel_allowed_or_text(username)
    if not allowed:
        await reply_md(message, f"⚠️ **Канал не разрешён**\n\n{reason}")
        return
    verified, verify_message = await verify_channel_for_user(username, message.from_user.id)
    if not verified and deps().settings.public_require_user_channel_admin and not is_system_admin(deps().settings, message.from_user.id):
        await reply_md(message, "⚠️ **Канал не подключён**\n\n" + verify_message + "\n\nДобавьте бота администратором с правом публикации и убедитесь, что пользователь является администратором канала.")
        return
    async with session_scope() as session:
        user = await get_or_create_user(session, message)
        try:
            await ensure_channel_claimable(session, user, username)
            channel = (await session.execute(select(Channel).where(Channel.owner_id == user.id, Channel.username == username))).scalar_one_or_none()
            if not channel:
                await ensure_channel_limit(session, user, deps().settings)
                channel = Channel(
                    owner_id=user.id,
                    username=username,
                    title=verify_message if verified else None,
                    topic=topic or "новости и полезный контент",
                    style="коротко, понятно, без кликбейта, с аккуратным markdown-оформлением",
                    min_sources=deps().settings.default_min_sources,
                    is_verified=verified,
                    media_enabled=deps().settings.media_enabled_by_default,
                )
                session.add(channel)
            else:
                channel.topic = topic or channel.topic
                channel.is_verified = verified
                channel.title = verify_message if verified else channel.title
            session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_channel_connected", details=f"{username}; verified={verified}"))
        except PublicSafetyError as exc:
            await reply_md(message, f"⚠️ **Не удалось подключить канал**\n\n{exc}")
            return
    await reply_md(message, f"✅ **Канал подключён**\n\nКанал: `{username}`\nПроверка прав: **{'пройдена' if verified else 'не пройдена'}**\n\nСледующий шаг: задайте редакционную политику обычным текстом, например: `Задай промпт для {username}: пиши кратко, строго, с источниками, без кликбейта`.")


async def _natural_check_channel(message: Message, username: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if not channel:
            await reply_md(message, f"⚠️ **Канал не привязан**\n\nСначала напишите: `Подключи канал {username}`.")
            return
    verified, info = await verify_channel_for_user(username, message.from_user.id)
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if channel:
            channel.is_verified = verified
            if verified:
                channel.title = info
            session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_channel_checked", details=f"{username}; verified={verified}; {info[:500]}"))
    if verified:
        await reply_md(message, f"✅ **Канал готов к работе**\n\nКанал: `{username}`\nПрава пользователя: **администратор/создатель**\nПрава бота: **публикация разрешена**")
    else:
        await reply_md(message, f"⚠️ **Проверка не пройдена**\n\nКанал: `{username}`\nПричина: {info}")


async def _natural_set_prompt(message: Message, username: str, prompt_text: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    prompt_text = (prompt_text or "").strip()
    if not prompt_text or len(prompt_text) < 20:
        await reply_md(message, f"⚠️ **Промпт слишком короткий**\n\nНапишите так: `Задай промпт для {username}: пиши про ... стиль ... запрещено ... цель ...`")
        return
    max_chars = deps().settings.editorial_prompt_max_chars
    if len(prompt_text) > max_chars:
        await reply_md(message, f"⚠️ **Промпт слишком длинный**\n\nСейчас {len(prompt_text)} символов, лимит {max_chars}.")
        return
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await reply_md(message, f"⚠️ **Не могу сохранить промпт**\n\n{error}")
            return
        channel.editorial_prompt = prompt_text
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_editorial_prompt_updated", details=f"{username}: {prompt_text[:700]}"))
    await reply_md(message, f"✅ **Промпт сохранён**\n\nКанал: `{username}`\nТеперь можно писать: `Сделай пост для {username} про ...`")


async def _natural_show_prompt(message: Message, username: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if not channel:
            await reply_md(message, "⚠️ **Канал не найден**")
            return
        text = channel.editorial_prompt or "Редакционная политика ещё не задана."
    await reply_md(message, f"🧠 **Промпт канала `{username}`**\n\n{text}")


async def _natural_set_format(message: Message, username: str, mode: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    mode = normalize_format_mode(mode or "markdown_v2")
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await reply_md(message, f"⚠️ **Не могу поменять формат**\n\n{error}")
            return
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        profile["format_mode"] = mode
        if mode == "markdown_v2":
            profile["formatting"] = "Telegram MarkdownV2: заголовки через **жирный**, короткие абзацы, без сырого HTML"
        channel.style_profile_json = json.dumps(profile, ensure_ascii=False)
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_format_mode_updated", details=f"{username}: {mode}"))
    await reply_md(message, f"✅ **Формат обновлён**\n\nКанал: `{username}`\nФормат постов: **{mode}**")


async def _natural_add_source(message: Message, username: str, url: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    url = (url or "").strip()
    if not url:
        await reply_md(message, "⚠️ **Не вижу RSS-ссылку**\n\nПришлите URL источника.")
        return
    ok, reason = is_safe_public_url(url, allow_localhost=not deps().settings.source_url_private_networks_blocked)
    if not ok:
        await reply_md(message, f"⚠️ **Источник отклонён**\n\n{reason}")
        return
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id, require_verified=False)
        if not channel:
            await reply_md(message, f"⚠️ **Канал не найден**\n\n{error}")
            return
        try:
            await ensure_source_limit(session, channel, deps().settings, adding=1)
            session.add(Source(channel_id=channel.id, url=url))
            await session.flush()
            session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_source_added", details=f"{username}: {url}"))
        except IntegrityError:
            await session.rollback()
            await reply_md(message, "ℹ️ **Источник уже был добавлен**")
            return
        except PublicSafetyError as exc:
            await reply_md(message, f"⚠️ **Лимит источников**\n\n{exc}")
            return
    await reply_md(message, f"✅ **Источник добавлен**\n\nКанал: `{username}`\nURL: `{url}`")


async def _natural_set_preset(message: Message, username: str, preset_key: str) -> None:
    if message.from_user is None:
        return
    preset = get_preset(preset_key)
    if not preset:
        await reply_md(message, "⚠️ **Не понял пресет**\n\nДоступно: `classic_news`, `meme_news`, `tech_ai`, `business`, `expert_blog`.")
        return
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id)
        if not channel:
            await reply_md(message, f"⚠️ **Канал не прошёл проверку**\n\n{error}")
            return
        apply_preset_to_channel(channel, preset.key, replace_prompt=False)
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_style_preset_applied", details=f"{username}: {preset.key}"))
    await reply_md(message, f"✅ **Пресет применён**\n\nКанал: `{username}`\nПресет: **{preset.title}**\n\n{preset.description}")


async def _natural_add_source_pack(message: Message, username: str, pack_key: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    pack_key = (pack_key or "").strip().lower()
    if not pack_key:
        await reply_md(message, "⚠️ **Не понял пакет источников**\n\nНапример: `добавь ru_world для @client_news`.")
        return
    registry = SourcePackRegistry()
    pack = registry.get(pack_key)
    if pack is None:
        await reply_md(message, "⚠️ **Такого пакета нет**\n\nНапишите `покажи пакеты источников` или используйте `/sourcepacks`.")
        return
    added_sources = 0
    updated_sources = 0
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id, require_verified=False)
        if not channel:
            await reply_md(message, f"⚠️ **Канал не найден**\n\n{error}")
            return
        new_urls = []
        for item in pack.sources:
            existing = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.url == item.url))).scalar_one_or_none()
            if not existing:
                new_urls.append(item.url)
        try:
            await ensure_source_limit(session, channel, deps().settings, adding=len(new_urls))
        except PublicSafetyError as exc:
            await reply_md(message, f"⚠️ **Лимит источников**\n\n{exc}")
            return
        for item in pack.sources:
            existing = (await session.execute(select(Source).where(Source.channel_id == channel.id, Source.url == item.url))).scalar_one_or_none()
            if existing:
                existing.title = item.title
                existing.priority = max(existing.priority or 0, item.priority)
                existing.enabled = True
                updated_sources += 1
            else:
                session.add(Source(channel_id=channel.id, url=item.url, title=item.title, priority=item.priority))
                added_sources += 1
            for rule_type in item.rules:
                if rule_type in {"trusted", "priority", "suspicious", "block"}:
                    exists_rule = (await session.execute(select(SourceRule).where(SourceRule.channel_id == channel.id, SourceRule.domain == item.domain, SourceRule.rule_type == rule_type))).scalar_one_or_none()
                    if not exists_rule:
                        session.add(SourceRule(channel_id=channel.id, domain=item.domain, rule_type=rule_type, note=f"natural source pack: {pack.key}"))
        session.add(SecurityEvent(owner_telegram_id=message.from_user.id, event_type="natural_source_pack_added", details=f"{username}: {pack_key}"))
    await reply_md(message, f"✅ **Пакет источников добавлен**\n\nКанал: `{username}`\nПакет: **{pack.key}**\nНовых RSS: **{added_sources}**, обновлено: **{updated_sources}**")


async def _natural_show_queue(message: Message, username: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if not channel:
            await reply_md(message, "⚠️ **Канал не найден**")
            return
        rows = (await session.execute(select(ScheduledPost, Draft).join(Draft, Draft.id == ScheduledPost.draft_id).where(ScheduledPost.channel_id == channel.id, ScheduledPost.status == "pending").order_by(ScheduledPost.run_at.asc()).limit(20))).all()
    if not rows:
        await reply_md(message, f"📭 **Очередь пустая**\n\nКанал: `{username}`")
        return
    lines = [f"• `#{sp.id}` — draft `{dr.id}` — {sp.run_at:%d.%m %H:%M} — {dr.kind}" for sp, dr in rows]
    await reply_md(message, f"📅 **Очередь публикаций `{username}`**\n\n" + "\n".join(lines))


async def _natural_show_calendar(message: Message, username: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    until = since + timedelta(days=8)
    async with session_scope() as session:
        channel = await find_owned_channel(session, message.from_user.id, username)
        if not channel:
            await reply_md(message, "⚠️ **Канал не найден**")
            return
        rows = (await session.execute(select(ScheduledPost, Draft).join(Draft, Draft.id == ScheduledPost.draft_id).where(ScheduledPost.channel_id == channel.id, ScheduledPost.run_at >= since, ScheduledPost.run_at <= until).order_by(ScheduledPost.run_at.asc()))).all()
    if not rows:
        await reply_md(message, f"📭 **Календарь пустой**\n\nКанал: `{username}`")
        return
    lines = [f"• {sp.run_at:%d.%m %H:%M} — **{sp.status}** — draft `{dr.id}` [{dr.kind}]" for sp, dr in rows]
    await reply_md(message, f"🗓 **Календарь `{username}`**\n\n" + "\n".join(lines))



async def _natural_show_usage(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        total_expr = func.coalesce(func.sum(AIUsageEvent.prompt_tokens_est + AIUsageEvent.completion_tokens_est), 0)
        calls = (await session.execute(select(func.count(AIUsageEvent.id)).where(AIUsageEvent.owner_telegram_id == message.from_user.id, AIUsageEvent.created_at >= today_start_utc()))).scalar_one()
        tokens = (await session.execute(select(total_expr).where(AIUsageEvent.owner_telegram_id == message.from_user.id, AIUsageEvent.cached.is_(False), AIUsageEvent.created_at >= today_start_utc()))).scalar_one()
    await reply_md(message, f"📊 **Расходы сегодня**\n\nAI-запросов: **{int(calls or 0)}** / `{deps().settings.max_ai_requests_per_day}`\nТокены est.: **{int(tokens or 0)}** / `{deps().settings.ai_daily_token_budget}`")


async def _natural_show_limits(message: Message) -> None:
    await cmd_limits(message)


async def _natural_show_audit(message: Message, username: str = "") -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        query = select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(12)
        if username:
            query = select(AuditEvent).where(AuditEvent.channel_username == normalize_channel_username(username)).order_by(desc(AuditEvent.created_at)).limit(12)
        rows = (await session.execute(query)).scalars().all()
    if not rows:
        await reply_md(message, "📭 **Audit пуст**")
        return
    lines = ["🧾 **Последние действия**", ""]
    for e in rows:
        lines.append(f"• {e.created_at:%d.%m %H:%M} — **{e.action_type}** — {e.channel_username or 'workspace'} — {e.result}")
    await reply_md(message, "\n".join(lines))


async def _natural_content_day(message: Message, username: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id, require_verified=False)
        if not channel:
            await reply_md(message, f"⚠️ **План недоступен**\n\n{error}")
            return
        try:
            profile = json.loads(channel.style_profile_json or "{}")
        except Exception:
            profile = {}
        preset = str(profile.get("preset") or profile.get("post_style") or "classic_news")
        plan = build_day_plan(channel.username, preset)
        await add_audit_event(session, user_id=message.from_user.id, role="owner" if message.from_user.id in deps().settings.admin_ids else "operator", channel=channel, action="content_day_plan", payload={"preset": preset})
    await reply_md(message, "🗓 **План контента**\n\n" + plan)


async def _natural_export_channel(message: Message, username: str) -> None:
    if message.from_user is None:
        return
    username = normalize_channel_username(username)
    async with session_scope() as session:
        channel, error = await require_safe_owned_channel_for_user(session, username, message.from_user.id, require_verified=False)
        if not channel:
            await reply_md(message, f"⚠️ **Экспорт недоступен**\n\n{error}")
            return
        sources = (await session.execute(select(Source).where(Source.channel_id == channel.id).order_by(Source.id))).scalars().all()
        rules = (await session.execute(select(SourceRule).where(SourceRule.channel_id == channel.id).order_by(SourceRule.id))).scalars().all()
        data = export_channel_config(channel, sources, rules)
    for chunk in split_long_message("```json\n" + data + "\n```"):
        await reply_md(message, chunk)


async def send_draft_preview(message: Message, draft_id: int, text: str, media_url: Optional[str] = None, media_type: Optional[str] = None, reply_to_message_id: Optional[int] = None, quality_score: int = 0, target_channel: Optional[str] = None) -> None:
    meta_lines = [f"📝 Черновик #{draft_id} готов к проверке"]
    if target_channel:
        meta_lines.append(f"Канал: {target_channel}")
    if quality_score:
        risk = "низкий" if quality_score >= 75 else "средний" if quality_score >= 55 else "высокий"
        meta_lines.append(f"Рейтинг новости: {quality_score}/100")
        meta_lines.append(f"Риск: {risk}")
    meta_lines.append(f"Медиа: {'есть' if media_url else 'нет'}")
    if reply_to_message_id:
        meta_lines.append(f"Обновление: ответом на message_id={reply_to_message_id}")
    meta_lines.append("Публикация: только после финального подтверждения")
    prefix = "\n".join(meta_lines) + "\n\n"

    # Show preview using the same Telegram formatting mode that will be used in the channel.
    format_mode = deps().settings.post_format_default
    try:
        async with session_scope() as session:
            draft = await session.get(Draft, draft_id)
            if draft:
                channel = await session.get(Channel, draft.channel_id)
                if channel:
                    format_mode = channel_format_mode(channel, deps().settings.post_format_default)
    except Exception:
        format_mode = deps().settings.post_format_default

    parse_mode = parse_mode_for_format(format_mode)
    formatted = format_for_telegram(prefix + text, format_mode)
    chunks = split_long_message(formatted)
    for chunk in chunks[:-1]:
        try:
            await message.answer(chunk, parse_mode=parse_mode)
        except Exception:
            await message.answer(strip_telegram_formatting(chunk))
    try:
        await message.answer(chunks[-1], parse_mode=parse_mode, reply_markup=draft_keyboard(draft_id))
    except Exception:
        await message.answer(strip_telegram_formatting(prefix + text), reply_markup=draft_keyboard(draft_id))


@router.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        return
    action = callback.data.split(":", 1)[1]
    if action == "help":
        await reply_md(callback.message, HELP_TEXT)
    elif action == "setup":
        async with session_scope() as session:
            channels = [ch.username for ch in await load_user_channels(session, callback.from_user.id)]
        await reply_md(callback.message, setup_master_text(channels))
    elif action == "channels":
        async with session_scope() as session:
            channels = await load_user_channels(session, callback.from_user.id)
        if not channels:
            await reply_md(callback.message, "📭 **Каналы не подключены**\n\nДобавьте бота администратором в канал и напишите: `Подключи канал @client_news`.")
        else:
            lines = ["📚 **Каналы рабочего пространства**", ""]
            for ch in channels:
                lines.append(f"• `{ch.username}` — verified: **{'да' if ch.is_verified else 'нет'}**, prompt: **{'да' if (ch.editorial_prompt or '').strip() else 'нет'}**")
            await reply_md(callback.message, "\n".join(lines))
    elif action == "packs":
        await reply_md(callback.message, "📦 **Пакеты источников**\n\nПосмотреть пакеты: `/sourcepacks`\nДобавить пакет: `/addpack @client_news ru_world`\nБыстрая настройка: `/quick @client_news ru_world`")
    elif action in {"budget", "usage"}:
        async with session_scope() as session:
            total_expr = func.coalesce(func.sum(AIUsageEvent.prompt_tokens_est + AIUsageEvent.completion_tokens_est), 0)
            today_used = (await session.execute(select(total_expr).where(AIUsageEvent.owner_telegram_id == callback.from_user.id, AIUsageEvent.cached.is_(False), AIUsageEvent.created_at >= today_start_utc()))).scalar_one()
            month_used = (await session.execute(select(total_expr).where(AIUsageEvent.owner_telegram_id == callback.from_user.id, AIUsageEvent.cached.is_(False), AIUsageEvent.created_at >= month_start_utc()))).scalar_one()
        await reply_md(callback.message, f"📊 **AI-расходы**\n\nСегодня: **{int(today_used or 0)}** / `{deps().settings.ai_daily_token_budget}` токенов est.\nМесяц: **{int(month_used or 0)}** / `{deps().settings.ai_monthly_token_budget}` токенов est.\n\nПодробнее: `/usage` и `/limits`.")
    elif action == "audit":
        await _natural_show_audit(callback.message)
    elif action == "doctor":
        await reply_md(callback.message, "🩺 **Диагностика**\n\nЗапустите `/doctor`. Если бот не публикует, сначала проверьте `/check @client_news`, `DATABASE_URL`, `ADMIN_IDS` и `ALLOWED_CHANNELS`.")
    elif action == "news_help":
        await reply_md(callback.message, "📰 **Одна новость**\n\nКоманда: `/news @client_news тема`\nОбычным текстом: `Найди актуальную новость для @client_news про рынок`")
    elif action == "digest_help":
        await reply_md(callback.message, "🗞 **Дайджест**\n\nКоманда: `/digest @client_news тема`\nОбычным текстом: `Сделай дайджест для @client_news по новостям отрасли`")
    elif action == "day_help":
        await reply_md(callback.message, "🗓 **День контента**\n\nКоманда: `/day @client_news`\nОбычным текстом: `Подготовь день контента для @client_news на завтра`")
    elif action == "demo":
        await reply_md(callback.message, demo_script_text())
    elif action == "offer":
        await reply_md(callback.message, commercial_offer_text() + "\n\n" + pricing_text())
    await callback.answer()


@router.callback_query(F.data.startswith("draft:"))
async def draft_callback(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        return
    _, action, raw_id = callback.data.split(":", maxsplit=2)
    draft_id = int(raw_id)
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        if not draft or draft.owner_telegram_id != callback.from_user.id:
            await callback.answer("Черновик не найден.", show_alert=True)
            return
        channel = await session.get(Channel, draft.channel_id)
        if not channel:
            await callback.answer("Канал не найден.", show_alert=True)
            return
        if action == "confirm":
            await callback.message.answer(
                f"Финальная проверка перед публикацией.\n\n"
                f"Канал: {channel.username}\n"
                f"Черновик: #{draft.id} [{draft.kind}]\n"
                f"Рейтинг: {draft.quality_score}/100\n"
                f"Медиа: {draft.media_type or 'нет'}\n\n"
                "Подтвердите публикацию только после проверки канала назначения, текста, источников и медиа. Это финальное подтверждение перед отправкой.",
                reply_markup=publish_confirm_keyboard(draft.id),
            )
            await callback.answer("Проверь канал")
            return
        if action == "cancelpub":
            await callback.message.answer(f"Публикация черновика #{draft.id} отменена. Черновик остался на месте.")
            await callback.answer("Отменено")
            return
        if action == "review":
            review = review_post_text(draft.text, require_source=(draft.kind in {"news", "digest", "update"} and public_post_sources_visible()), digest=draft.kind == "digest")
            draft.risk_notes = (draft.risk_notes or "") + "\n" + review.summary()
            await callback.message.answer(review.summary())
            await callback.answer("Проверено")
            return
        if action == "publish":
            try:
                user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
                role = user_role(deps().settings, user, callback.from_user.id)
                if not role_can(role, "publish"):
                    await add_audit_event(session, user_id=callback.from_user.id, role=role, channel=channel, action="publish_blocked", payload={"draft_id": draft.id}, result="blocked", reason="insufficient_role", draft_id=draft.id)
                    raise ValueError("Недостаточно прав для публикации")
                ok, reason = await verify_channel_for_user(channel.username, callback.from_user.id)
                if not ok:
                    await add_audit_event(session, user_id=callback.from_user.id, role=role, channel=channel, action="publish_blocked", payload={"draft_id": draft.id}, result="blocked", reason=reason, draft_id=draft.id)
                    raise ValueError(reason)
                await deps().publisher.publish_draft(session, draft)
                await add_audit_event(session, user_id=callback.from_user.id, role=role, channel=channel, action="publish", payload={"draft_id": draft.id, "kind": draft.kind}, result="success", draft_id=draft.id)
            except Exception as exc:
                await callback.message.answer(f"Не удалось опубликовать: {exc}")
                await callback.answer()
                return
            await callback.message.answer(f"Готово. Черновик #{draft.id} опубликован в {channel.username}.")
            await callback.answer("Опубликовано")
            return
        if action in {"rewrite", "rewrite_short", "format", "media_style", "meme_style"}:
            instruction = "сделай пост живее, яснее и короче"
            answer_text = "Переписал"
            if action == "rewrite_short":
                instruction = "сократи пост на 25-40%, оставь смысл, убери воду, не добавляй новых фактов"
                answer_text = "Сократил"
            elif action == "format":
                instruction = "улучши Telegram-оформление: сильный жирный заголовок, короткие абзацы, аккуратные выделения, без новых фактов"
                answer_text = "Оформил"
            elif action == "media_style":
                draft.text = await deps().writer.rewrite_as_media_post(channel, draft.text, owner_telegram_id=callback.from_user.id if callback.from_user else 0)
                answer_text = "Сделал как СМИ"
                await session.flush()
                await send_draft_preview(callback.message, draft.id, draft.text, media_url=draft.media_url, media_type=draft.media_type, reply_to_message_id=draft.reply_to_message_id, quality_score=draft.quality_score)
                await callback.answer(answer_text)
                return
            elif action == "meme_style":
                instruction = "сделай живой Telegram-стиль: короткий жирный заголовок, один цитатный блок, лёгкая ирония, можно одно зачёркивание или spoiler, без новых фактов, источники не трогай"
                answer_text = "Сделал живее"
            draft.text = await deps().writer.rewrite_post(channel, draft.text, instruction, owner_telegram_id=callback.from_user.id if callback.from_user else 0)
            await session.flush()
            await send_draft_preview(callback.message, draft.id, draft.text, media_url=draft.media_url, media_type=draft.media_type, reply_to_message_id=draft.reply_to_message_id, quality_score=draft.quality_score)
            await callback.answer(answer_text)
            return
        if action == "schedule1h":
            try:
                await ensure_schedule_limit(session, channel, deps().settings)
            except PublicSafetyError as exc:
                await callback.message.answer(str(exc))
                await callback.answer()
                return
            draft.status = "scheduled"
            run_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
            session.add(ScheduledPost(draft_id=draft.id, channel_id=draft.channel_id, run_at=run_at))
            user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
            await add_audit_event(session, user_id=callback.from_user.id, role=user_role(deps().settings, user, callback.from_user.id), channel=channel, action="schedule", payload={"draft_id": draft.id, "run_at": run_at.isoformat()}, draft_id=draft.id)
            await callback.message.answer(f"Черновик #{draft.id} запланирован через 1 час для {channel.username}.")
            await callback.answer("Запланировано")
            return
        if action == "delete":
            draft.status = "deleted"
            user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
            await add_audit_event(session, user_id=callback.from_user.id, role=user_role(deps().settings, user, callback.from_user.id), channel=channel, action="delete_draft", payload={"draft_id": draft.id}, draft_id=draft.id)
            await callback.message.answer(f"Черновик #{draft.id} удалён.")
            await callback.answer("Удалено")
            return
    await callback.answer()
