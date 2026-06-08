from datetime import datetime, timezone

from app.config import Settings
from app.services.news_collector import Article
from app.services.source_checker import SourceChecker


def test_source_checker_high_confidence():
    settings = Settings(TELEGRAM_BOT_TOKEN="x", DEFAULT_MIN_SOURCES=2)
    checker = SourceChecker(settings)
    articles = [
        Article("A", "https://site-a.com/a", "summary " * 30, "A", datetime.now(timezone.utc)),
        Article("B", "https://site-b.com/b", "summary " * 30, "B", datetime.now(timezone.utc)),
    ]
    result = checker.check(articles, min_sources=2)
    assert result.confidence == "high"
    assert len(result.accepted) == 2
    assert result.top_score >= 70


def test_source_checker_blocks_domain():
    settings = Settings(TELEGRAM_BOT_TOKEN="x")
    checker = SourceChecker(settings)
    result = checker.check([Article("A", "https://bad.com/a", "summary", "Bad", datetime.now(timezone.utc))], blocked_domains={"bad.com"})
    assert result.confidence == "none"
    assert not result.accepted


def test_source_checker_no_sources():
    settings = Settings(TELEGRAM_BOT_TOKEN="x")
    checker = SourceChecker(settings)
    result = checker.check([])
    assert result.confidence == "none"
    assert not result.accepted
