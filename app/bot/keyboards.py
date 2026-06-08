from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Проверить публикацию", callback_data=f"draft:confirm:{draft_id}")],
            [
                InlineKeyboardButton(text="✏️ Живее", callback_data=f"draft:rewrite:{draft_id}"),
                InlineKeyboardButton(text="🧼 Короче", callback_data=f"draft:rewrite_short:{draft_id}"),
            ],
            [
                InlineKeyboardButton(text="✨ Оформить", callback_data=f"draft:format:{draft_id}"),
                InlineKeyboardButton(text="🗞 Как СМИ", callback_data=f"draft:media_style:{draft_id}"),
                InlineKeyboardButton(text="😈 Live", callback_data=f"draft:meme_style:{draft_id}"),
            ],
            [InlineKeyboardButton(text="🧪 Проверка", callback_data=f"draft:review:{draft_id}"), InlineKeyboardButton(text="📅 Через 1 час", callback_data=f"draft:schedule1h:{draft_id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"draft:delete:{draft_id}")],
        ]
    )


def publish_confirm_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, опубликовать", callback_data=f"draft:publish:{draft_id}")],
            [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"draft:cancelpub:{draft_id}")],
        ]
    )


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧭 Мастер", callback_data="menu:setup"), InlineKeyboardButton(text="📚 Каналы", callback_data="menu:channels")],
            [InlineKeyboardButton(text="📰 Новость", callback_data="menu:news_help"), InlineKeyboardButton(text="🗞 Дайджест", callback_data="menu:digest_help")],
            [InlineKeyboardButton(text="🗓 День контента", callback_data="menu:day_help"), InlineKeyboardButton(text="📦 Источники", callback_data="menu:packs")],
            [InlineKeyboardButton(text="📊 Расходы", callback_data="menu:usage"), InlineKeyboardButton(text="🧾 Audit", callback_data="menu:audit")],
            [InlineKeyboardButton(text="🎬 Демо", callback_data="menu:demo"), InlineKeyboardButton(text="💼 Оффер", callback_data="menu:offer")],
            [InlineKeyboardButton(text="🩺 Диагностика", callback_data="menu:doctor"), InlineKeyboardButton(text="📚 Помощь", callback_data="menu:help")],
        ]
    )
