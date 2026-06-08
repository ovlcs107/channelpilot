from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Channel, Draft, ScheduledPost, Source, User
from app.services.ai_cost import today_start_utc


class PublicSafetyError(ValueError):
    """User-facing validation error for multi-tenant/public bot safety."""


def is_system_admin(settings: Settings, telegram_id: int | None) -> bool:
    return bool(telegram_id is not None and telegram_id in set(settings.admin_ids))


async def ensure_user_not_blocked(user: User) -> None:
    if getattr(user, "is_blocked", False):
        raise PublicSafetyError("Аккаунт заблокирован владельцем бота. Если это ошибка — обратитесь в поддержку.")


async def ensure_terms_accepted(user: User, settings: Settings) -> None:
    if settings.public_require_terms_acceptance and not user.accepted_terms_at:
        raise PublicSafetyError("Сначала примите правила: /accept. Без этого управление каналами недоступно.")


async def ensure_channel_claimable(session: AsyncSession, user: User, username: str) -> None:
    existing = (await session.execute(select(Channel).where(Channel.username == username, Channel.owner_id != user.id))).scalar_one_or_none()
    if existing:
        raise PublicSafetyError(
            f"Канал {username} уже привязан к другому аккаунту. "
            "Чтобы не было случайного захвата каналов, один @channel может иметь только одного владельца в боте."
        )


async def ensure_channel_limit(session: AsyncSession, user: User, settings: Settings) -> None:
    if is_system_admin(settings, user.telegram_id):
        return
    if settings.public_max_channels_per_user <= 0:
        return
    count = (await session.execute(select(func.count(Channel.id)).where(Channel.owner_id == user.id))).scalar_one()
    if int(count or 0) >= settings.public_max_channels_per_user:
        raise PublicSafetyError(
            f"Лимит каналов на пользователя: {settings.public_max_channels_per_user}. "
            "Для большего количества нужна отдельная выдача доступа/тариф."
        )


async def ensure_source_limit(session: AsyncSession, channel: Channel, settings: Settings, adding: int = 1) -> None:
    if settings.public_max_sources_per_channel <= 0:
        return
    count = (await session.execute(select(func.count(Source.id)).where(Source.channel_id == channel.id))).scalar_one()
    if int(count or 0) + adding > settings.public_max_sources_per_channel:
        raise PublicSafetyError(
            f"Лимит RSS-источников на канал: {settings.public_max_sources_per_channel}. "
            "Удалите лишние источники или увеличьте лимит в переменных окружения."
        )


async def ensure_daily_draft_limit(session: AsyncSession, telegram_id: int, settings: Settings, adding: int = 1) -> None:
    if is_system_admin(settings, telegram_id):
        return
    if settings.public_max_drafts_per_day <= 0:
        return
    count = (
        await session.execute(
            select(func.count(Draft.id)).where(Draft.owner_telegram_id == telegram_id, Draft.created_at >= today_start_utc())
        )
    ).scalar_one()
    if int(count or 0) + adding > settings.public_max_drafts_per_day:
        raise PublicSafetyError(
            f"Дневной лимит черновиков: {settings.public_max_drafts_per_day}. "
            "Это защита от слива AI-бюджета и спама. Завтра лимит обновится."
        )


async def ensure_schedule_limit(session: AsyncSession, channel: Channel, settings: Settings) -> None:
    if settings.public_max_scheduled_per_channel <= 0:
        return
    count = (
        await session.execute(
            select(func.count(ScheduledPost.id)).where(
                ScheduledPost.channel_id == channel.id,
                ScheduledPost.status == "pending",
            )
        )
    ).scalar_one()
    if int(count or 0) >= settings.public_max_scheduled_per_channel:
        raise PublicSafetyError(
            f"Лимит очереди на канал: {settings.public_max_scheduled_per_channel} постов. "
            "Сначала опубликуйте или отмените старые задачи."
        )


def ensure_network_size(user_id: int, usernames: list[str], settings: Settings) -> None:
    if is_system_admin(settings, user_id):
        return
    if settings.public_max_network_channels_per_request > 0 and len(usernames) > settings.public_max_network_channels_per_request:
        raise PublicSafetyError(
            f"За один сетевой пост можно максимум {settings.public_max_network_channels_per_request} каналов. "
            "Так безопаснее и экономнее по AI-токенам."
        )


def ensure_public_auto_allowed(user_id: int, settings: Settings) -> None:
    if is_system_admin(settings, user_id):
        return
    if not settings.public_allow_auto_publish:
        raise PublicSafetyError(
            "Полный auto-режим в публичном доступе выключен. Используйте safe/semi: бот готовит черновик, администратор подтверждает публикацию. "
            "Автопостинг можно включить только админам или на отдельном тарифе."
        )
