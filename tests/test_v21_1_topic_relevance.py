from datetime import datetime, timezone

from app.config import Settings
from app.services.news_collector import Article
from app.services.source_checker import SourceChecker


def make_article(title: str, summary: str, source: str = "Example", url: str = "https://example.com/news") -> Article:
    return Article(
        title=title,
        url=url,
        summary=summary,
        source_name=source,
        published_at=datetime.now(timezone.utc),
        media_url="https://example.com/image.jpg",
        media_type="photo",
    )


def test_rejects_politics_for_telegram_ai_media_query() -> None:
    checker = SourceChecker(Settings())
    article = make_article(
        "Лавров о политике ЕС: враг найден",
        "Сергей Лавров прокомментировал отношения России и Евросоюза, санкции и международную политику.",
        url="https://example.com/politics/lavrov",
    )
    result = checker.check(
        [article],
        min_sources=1,
        query="Telegram, AI и автоматизацию медиа",
        channel_topic="Telegram-каналы, AI, SMM, медиа и автоматизация контента",
        editorial_prompt="Не использовать политические новости, если они не связаны с Telegram, AI или SMM.",
    )
    assert result.accepted == []
    assert result.rejection_reasons.get("не по теме запроса/канала") == 1


def test_accepts_ai_telegram_media_article_for_channel() -> None:
    checker = SourceChecker(Settings())
    article = make_article(
        "Telegram добавил новые инструменты для автоматизации каналов",
        "Обновление помогает администраторам каналов быстрее готовить контент, использовать AI-инструменты и управлять медиа-публикациями.",
        source="Telegram Blog",
        url="https://telegram.org/blog/channel-ai-tools",
    )
    result = checker.check(
        [article],
        min_sources=1,
        query="Telegram, AI и автоматизацию медиа",
        channel_topic="Telegram-каналы, AI, SMM, медиа и автоматизация контента",
    )
    assert len(result.accepted) == 1
    assert result.scores[0].topic_score >= 45


def test_query_relevance_affects_score_not_only_source_quality() -> None:
    checker = SourceChecker(Settings())
    relevant = make_article(
        "OpenAI запустила инструмент для SMM-команд",
        "Сервис помогает маркетологам и медиа-командам автоматизировать подготовку постов для Telegram и других каналов.",
        source="Tech Source",
        url="https://example.com/tech/openai-smm",
    )
    off_topic = make_article(
        "Грузовик Валдай показали на выставке",
        "Компания представила новую модель коммерческого транспорта на отраслевой выставке.",
        source="Auto Source",
        url="https://example.com/auto/valday",
    )
    result = checker.check(
        [off_topic, relevant],
        min_sources=1,
        query="AI SMM Telegram медиа",
        channel_topic="AI, Telegram, SMM, медиа",
    )
    assert [a.url for a in result.accepted] == [relevant.url]
