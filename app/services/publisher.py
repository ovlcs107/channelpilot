from __future__ import annotations

import logging
from typing import Iterable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message as TelegramMessage, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Draft, PublishedPost
from app.services.media_manager import MediaManager
from app.utils.text import split_long_message, truncate
from app.services.telegram_formatting import (
    channel_format_mode,
    format_for_telegram,
    parse_mode_for_format,
    strip_telegram_formatting,
)

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(
        self,
        bot: Bot,
        caption_limit: int = 1024,
        media_manager: MediaManager | None = None,
        require_channel_verification: bool = True,
        require_user_admin_on_publish: bool = True,
        allowed_channels: Iterable[str] | None = None,
        default_format_mode: str = "html",
        link_preview_enabled: bool = True,
    ) -> None:
        self.bot = bot
        self.caption_limit = caption_limit
        self.media_manager = media_manager
        self.require_channel_verification = require_channel_verification
        self.require_user_admin_on_publish = require_user_admin_on_publish
        self.allowed_channels = {self._normalize_channel_name(ch) for ch in (allowed_channels or []) if ch}
        self.default_format_mode = default_format_mode
        self.link_preview_enabled = link_preview_enabled

    @staticmethod
    def _normalize_channel_name(username: str) -> str:
        value = (username or "").strip().lower()
        if value and not value.startswith("@"):
            value = "@" + value
        return value

    def _ensure_channel_allowed(self, username: str) -> None:
        if not self.allowed_channels:
            return
        normalized = self._normalize_channel_name(username)
        if normalized not in self.allowed_channels:
            raise ValueError(f"Канал {username} не входит в ALLOWED_CHANNELS. Публикация заблокирована private-режимом.")

    async def publish_draft(self, session: AsyncSession, draft: Draft) -> int:
        channel = await session.get(Channel, draft.channel_id)
        if channel is None:
            raise ValueError("Канал для черновика не найден.")
        self._ensure_channel_allowed(channel.username)
        if self.require_channel_verification and not channel.is_verified:
            raise ValueError("Канал не проверен. Сначала выполните /check @channel и убедитесь, что бот является администратором с правом публикации.")
        if self.require_user_admin_on_publish and draft.owner_telegram_id:
            ok, reason = await self.verify_channel_access(channel.username, draft.owner_telegram_id)
            if not ok:
                raise ValueError("Публикация остановлена: " + reason)

        root_message = await self._send_post(channel, draft)
        draft.status = "published"
        published = PublishedPost(
            channel_id=channel.id,
            draft_id=draft.id,
            message_id=root_message.message_id,
            kind=draft.kind,
            reply_to_message_id=draft.reply_to_message_id,
            article_key=draft.primary_article_key,
            topic_key=draft.primary_article_topic_key,
            article_url=draft.primary_article_url,
            article_title=draft.primary_article_title,
            content_hash=draft.primary_article_hash,
            media_url=draft.media_url,
            media_type=draft.media_type,
        )
        session.add(published)
        await session.flush()
        logger.info("Draft %s published to %s as message %s", draft.id, channel.username, root_message.message_id)
        return root_message.message_id

    async def _send_post(self, channel: Channel, draft: Draft) -> TelegramMessage:
        media_url = draft.media_url if channel.media_enabled else None
        media_type = draft.media_type if channel.media_enabled else None
        if media_url and media_type in {"photo", "video"}:
            try:
                media_payload = media_url
                actual_type = media_type
                if self.media_manager:
                    downloaded = await self.media_manager.prepare_media(media_url, media_type)
                    if downloaded:
                        media_payload = FSInputFile(downloaded.path)
                        actual_type = downloaded.media_type
                return await self._send_media_post(channel, draft.text, media_payload, actual_type, draft.reply_to_message_id)
            except Exception as exc:
                logger.warning("Media publish failed, fallback to text post: %s", exc)
                fallback_text = draft.text
                if media_url:
                    fallback_text += f"\n\nМедиа: {media_url}"
                return await self._send_text_post(channel, fallback_text, draft.reply_to_message_id)
        return await self._send_text_post(channel, draft.text, draft.reply_to_message_id)

    async def _send_text_post(self, channel: Channel, text: str, reply_to_message_id: Optional[int]) -> TelegramMessage:
        chat_id = channel.username
        format_mode = channel_format_mode(channel, self.default_format_mode)
        chunks = split_long_message(text)
        root = await self._send_message(chat_id, chunks[0], reply_to_message_id=reply_to_message_id, format_mode=format_mode)
        for chunk in chunks[1:]:
            await self._send_message(chat_id, chunk, reply_to_message_id=root.message_id, format_mode=format_mode)
        return root

    async def _send_media_post(
        self,
        channel: Channel,
        text: str,
        media_payload,
        media_type: str,
        reply_to_message_id: Optional[int],
    ) -> TelegramMessage:
        chat_id = channel.username
        format_mode = channel_format_mode(channel, self.default_format_mode)
        chunks = split_long_message(text, limit=max(200, self.caption_limit - 10))
        caption = chunks[0]
        if media_type == "video":
            root = await self._send_video(chat_id, media_payload, caption, reply_to_message_id, format_mode=format_mode)
        else:
            root = await self._send_photo(chat_id, media_payload, caption, reply_to_message_id, format_mode=format_mode)
        for chunk in chunks[1:]:
            await self._send_message(chat_id, chunk, reply_to_message_id=root.message_id, format_mode=format_mode)
        return root

    async def _send_message(self, chat_id: str, text: str, reply_to_message_id: Optional[int] = None, format_mode: str | None = None) -> TelegramMessage:
        parse_mode = parse_mode_for_format(format_mode or self.default_format_mode)
        formatted_text = format_for_telegram(text, format_mode or self.default_format_mode)
        kwargs = {"chat_id": chat_id, "text": formatted_text, "disable_web_page_preview": not self.link_preview_enabled}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if reply_to_message_id:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to_message_id)
        try:
            return await self.bot.send_message(**kwargs)
        except TypeError:
            kwargs.pop("reply_parameters", None)
            if reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
            return await self.bot.send_message(**kwargs)
        except TelegramBadRequest as exc:
            logger.warning("Formatted/reply message failed, retrying safely: %s", exc)
            # 1) Retry without reply target, because old/deleted message_id can break replies.
            no_reply = dict(kwargs)
            no_reply.pop("reply_parameters", None)
            no_reply.pop("reply_to_message_id", None)
            try:
                return await self.bot.send_message(**no_reply)
            except TelegramBadRequest:
                # 2) Retry as plain text, because bad AI markup must never block publication.
                no_reply["text"] = strip_telegram_formatting(text)
                no_reply.pop("parse_mode", None)
                return await self.bot.send_message(**no_reply)

    async def _send_photo(self, chat_id: str, photo, caption: str, reply_to_message_id: Optional[int], format_mode: str | None = None) -> TelegramMessage:
        parse_mode = parse_mode_for_format(format_mode or self.default_format_mode)
        formatted_caption = truncate(format_for_telegram(caption, format_mode or self.default_format_mode), self.caption_limit)
        kwargs = {"chat_id": chat_id, "photo": photo, "caption": formatted_caption}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if reply_to_message_id:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to_message_id)
        try:
            return await self.bot.send_photo(**kwargs)
        except TypeError:
            kwargs.pop("reply_parameters", None)
            if reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
            return await self.bot.send_photo(**kwargs)
        except TelegramBadRequest as exc:
            logger.warning("Formatted photo caption failed, retrying as plain text: %s", exc)
            kwargs["caption"] = truncate(strip_telegram_formatting(caption), self.caption_limit)
            kwargs.pop("parse_mode", None)
            kwargs.pop("reply_parameters", None)
            kwargs.pop("reply_to_message_id", None)
            return await self.bot.send_photo(**kwargs)

    async def _send_video(self, chat_id: str, video, caption: str, reply_to_message_id: Optional[int], format_mode: str | None = None) -> TelegramMessage:
        parse_mode = parse_mode_for_format(format_mode or self.default_format_mode)
        formatted_caption = truncate(format_for_telegram(caption, format_mode or self.default_format_mode), self.caption_limit)
        kwargs = {"chat_id": chat_id, "video": video, "caption": formatted_caption}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if reply_to_message_id:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to_message_id)
        try:
            return await self.bot.send_video(**kwargs)
        except TypeError:
            kwargs.pop("reply_parameters", None)
            if reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
            return await self.bot.send_video(**kwargs)
        except TelegramBadRequest as exc:
            logger.warning("Formatted video caption failed, retrying as plain text: %s", exc)
            kwargs["caption"] = truncate(strip_telegram_formatting(caption), self.caption_limit)
            kwargs.pop("parse_mode", None)
            kwargs.pop("reply_parameters", None)
            kwargs.pop("reply_to_message_id", None)
            return await self.bot.send_video(**kwargs)

    async def verify_channel_access(self, channel_username: str, requester_telegram_id: int | None = None) -> tuple[bool, str]:
        """Verify both bot posting rights and optional human owner/admin rights.

        This is critical for public mode: a user may only bind/publish to a
        channel if the bot can post there AND the requesting user is an admin
        or creator of that exact channel.
        """
        bot_ok, bot_info = await self.verify_channel_permissions(channel_username)
        if not bot_ok:
            return False, bot_info
        if requester_telegram_id is None:
            return True, bot_info
        try:
            admins = await self.bot.get_chat_administrators(channel_username)
            for admin in admins:
                if admin.user.id == requester_telegram_id and admin.status in {"administrator", "creator"}:
                    return True, bot_info
            return False, "Telegram-аккаунт пользователя не найден среди администраторов этого канала. Добавьте пользователя администратором/создателем или подключите корректный канал."
        except Exception as exc:
            return False, f"Не удалось проверить права пользователя в канале: {exc}"

    async def verify_channel_permissions(self, channel_username: str) -> tuple[bool, str]:
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(channel_username, me.id)
            can_post = bool(getattr(member, "can_post_messages", False))
            is_admin = member.status in {"administrator", "creator"}
            if is_admin and can_post:
                chat = await self.bot.get_chat(channel_username)
                return True, chat.title or channel_username
            if is_admin:
                return False, "Бот администратор, но без права публикации сообщений."
            return False, "Бот не является администратором канала."
        except Exception as exc:
            return False, f"Не удалось проверить канал: {exc}"
