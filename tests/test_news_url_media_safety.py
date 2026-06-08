from app.services.news_collector import (
    _entry_article_link,
    _is_probably_media_resource_url,
    _pick_media,
)


def test_kommersant_socialpic_is_not_article_url():
    assert _is_probably_media_resource_url("https://iv.kommersant.ru/SocialPics/871203_49_0_936547521") is True


def test_entry_prefers_html_article_over_media_link():
    entry = {
        "link": "https://iv.kommersant.ru/SocialPics/871203_49_0_936547521",
        "links": [
            {"rel": "enclosure", "type": "image/jpeg", "href": "https://iv.kommersant.ru/SocialPics/871203_49_0_936547521"},
            {"rel": "alternate", "type": "text/html", "href": "https://www.kommersant.ru/doc/871203"},
        ],
    }
    assert _entry_article_link(entry) == "https://www.kommersant.ru/doc/871203"


def test_unknown_or_logo_media_is_not_attached():
    assert _pick_media([("https://example.com/assets/logo.png", "image/png")]) == (None, None)
    assert _pick_media([("https://example.com/random-no-extension", None)]) == (None, None)
