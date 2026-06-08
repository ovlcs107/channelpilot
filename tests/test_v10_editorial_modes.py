from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.models import Channel
from app.services.ai_writer import AIWriter
from app.services.news_collector import Article


@pytest.mark.asyncio
async def test_story_mode_uses_one_news_item_without_ai_key():
    writer = AIWriter(Settings(AI_API_KEY=""))
    channel = Channel(owner_id=1, username="@demo", topic="игры", style="коротко")
    articles = [
        Article("новый продукт снова в центре внимания", "https://example.com/a", "Фанаты обсуждают новую волну интереса к игре.", "Example", datetime.now(timezone.utc)),
        Article("Control получила дату", "https://example.com/b", "Remedy раскрыла детали проекта.", "Example 2", datetime.now(timezone.utc)),
    ]

    text = await writer.write_news_post(channel, articles, "medium", "тест", post_mode="story")

    assert "новый продукт" in text
    assert "Control получила дату" not in text
    assert "🔗 Источник:" not in text
    assert "Подписаться на [TG Media Lab](https://t.me/TMediaLabG)" in text


@pytest.mark.asyncio
async def test_digest_mode_keeps_multiple_news_items_without_ai_key():
    writer = AIWriter(Settings(AI_API_KEY=""))
    channel = Channel(owner_id=1, username="@demo", topic="игры", style="коротко")
    articles = [
        Article("новый продукт снова в центре внимания", "https://example.com/a", "Фанаты обсуждают новую волну интереса к игре.", "Example", datetime.now(timezone.utc)),
        Article("Control получила дату", "https://example.com/b", "Remedy раскрыла детали проекта.", "Example 2", datetime.now(timezone.utc)),
    ]

    text = await writer.write_news_post(channel, articles, "medium", "тест", post_mode="digest")

    assert "Короткий дайджест" in text
    assert "новый продукт" in text
    assert "Control получила дату" in text
    assert "🔗 Источники:" not in text
    assert "Подписаться на [TG Media Lab](https://t.me/TMediaLabG)" in text
