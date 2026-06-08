from app.services.news_collector import Article, _extract_meta_media, normalize_article_url


def test_normalize_article_url_removes_tracking_params():
    assert normalize_article_url("https://www.example.com/news/?utm_source=tg&utm=1&id=5#top") == "https://example.com/news?id=5"


def test_content_hash_changes_when_article_summary_updates():
    old = Article("Title", "https://example.com/news", "old summary", "Example")
    new = Article("Title", "https://example.com/news", "new summary", "Example")
    assert old.article_key == new.article_key
    assert old.content_hash != new.content_hash


def test_extract_meta_media_prefers_og_image():
    html = '<html><head><meta property="og:image" content="/cover.jpg"></head></html>'
    media_url, media_type = _extract_meta_media(html, "https://example.com/post")
    assert media_url == "https://example.com/cover.jpg"
    assert media_type == "photo"


def test_topic_key_matches_similar_titles():
    a = Article("новый продукт получила новый трейлер", "https://a.com/1", "", "A")
    b = Article("Новый трейлер новый продукт", "https://b.com/2", "", "B")
    assert a.topic_key == b.topic_key
