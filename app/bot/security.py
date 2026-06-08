from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select

from app.config import Settings
from app.database import session_scope
from app.models import SecurityEvent, User
from app.services.telegram_formatting import format_dm_markdown, strip_telegram_formatting

logger = logging.getLogger(__name__)

PUBLIC_COMMANDS = {"start", "help", "status", "sourcepacks", "menu", "terms", "accept"}
ADMIN_COMMANDS = {"adminusers", "adminchannels", "ban", "unban", "securitylog"}


class AccessControlMiddleware(BaseMiddleware):
    """Access control for both private and public private modes.

    In public mode random users are allowed to use the bot only after /accept,
    but admin commands always stay owner-only. In private/admin modes only
    ADMIN_IDS can execute protected commands.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._minute_hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        command = self._extract_command(event)
        if not self._allowed_by_mode(user.id, command):
            await self._deny(event, user.id, "⛔ Доступ закрыт. Бот в private/admin_only режиме.")
            return None

        if command in ADMIN_COMMANDS and user.id not in set(self.settings.admin_ids):
            await self._deny(event, user.id, "⛔ Это команда владельца бота.")
            return None

        if not await self._user_account_allowed(event, user.id, command):
            return None

        if self.settings.rate_limit_enabled and command not in PUBLIC_COMMANDS:
            ok, retry_after = self._rate_limited(user.id)
            if not ok:
                if isinstance(event, CallbackQuery):
                    await event.answer(f"Слишком часто. Подожди {retry_after} сек.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(f"Слишком много действий подряд. Подождите {retry_after} сек.")
                return None

        return await handler(event, data)

    def _allowed_by_mode(self, user_id: int, command: str | None) -> bool:
        mode = self.settings.bot_access_mode.lower().strip()
        if command in PUBLIC_COMMANDS:
            return True
        if user_id in set(self.settings.admin_ids):
            return True
        if mode in {"open", "public"}:
            return True
        if mode in {"admin_only", "private", "closed"}:
            return False
        return False

    async def _user_account_allowed(self, event: TelegramObject, user_id: int, command: str | None) -> bool:
        # Admins are never blocked by public onboarding gates.
        if user_id in set(self.settings.admin_ids):
            return True
        if command in PUBLIC_COMMANDS:
            return True
        mode = self.settings.bot_access_mode.lower().strip()
        if mode not in {"open", "public"}:
            return True
        try:
            async with session_scope() as session:
                db_user = (await session.execute(select(User).where(User.telegram_id == user_id))).scalar_one_or_none()
                if db_user and db_user.is_blocked:
                    await self._deny(event, user_id, f"⛔ Аккаунт заблокирован. {db_user.blocked_reason or ''}".strip())
                    return False
                if self.settings.public_require_terms_acceptance and (not db_user or not db_user.accepted_terms_at):
                    await self._deny(event, user_id, "Сначала примите правила: /accept. Это защита каналов и AI-бюджета.")
                    return False
        except Exception as exc:
            logger.warning("Public account gate failed for %s: %s", user_id, exc)
            await self._deny(event, user_id, "Временная ошибка проверки доступа. Попробуйте ещё раз через минуту.")
            return False
        return True

    async def _deny(self, event: TelegramObject, user_id: int, text: str) -> None:
        details = text
        try:
            async with session_scope() as session:
                session.add(SecurityEvent(owner_telegram_id=user_id, event_type="access_denied", details=details[:1000]))
        except Exception:
            pass
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            suffix = ""
            if "private/admin_only" in text:
                suffix = (
                    f"\n\nTelegram ID: `{user_id}`\n"
                    "Добавьте его в переменную ADMIN_IDS на Railway, например:\n"
                    f"ADMIN_IDS={user_id}"
                )
            body = text + suffix
            try:
                await event.answer(format_dm_markdown(body), parse_mode="MarkdownV2")
            except Exception as exc:
                logger.warning("Access denied MarkdownV2 send failed, falling back to plain text: %s", exc)
                await event.answer(strip_telegram_formatting(body))

    def _rate_limited(self, user_id: int) -> tuple[bool, int]:
        now = time.monotonic()
        window = self.settings.rate_limit_window_seconds
        limit = self.settings.max_commands_per_window
        q = self._minute_hits[user_id]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(window - (now - q[0])))
            return False, retry
        q.append(now)
        return True, 0

    @staticmethod
    def _extract_command(event: TelegramObject) -> str | None:
        text = None
        if isinstance(event, Message):
            text = event.text or event.caption
        elif isinstance(event, CallbackQuery):
            return None
        if not text or not text.startswith("/"):
            return None
        return text.split(maxsplit=1)[0].split("@", 1)[0].lstrip("/").lower()
