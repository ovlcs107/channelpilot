from app.services.news_collector import Article, NewsCollector
from app.config import Settings


def test_dedupe_and_sort():
    collector = NewsCollector(Settings(TELEGRAM_BOT_TOKEN="x"))
    articles = [
        Article("Same", "https://example.com/a", "", "src"),
        Article("Same", "https://example.com/a?utm=1", "", "src"),
        Article("Other", "https://another.com/b", "", "src"),
    ]
    result = collector._dedupe_and_sort(articles)
    assert len(result) == 2
