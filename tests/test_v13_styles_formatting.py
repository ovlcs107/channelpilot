from app.services.style_presets import get_preset, list_preset_lines
from app.services.telegram_formatting import markdown_to_telegram_v2, markdownish_to_html, sanitize_telegram_html
from app.services.editorial_review import review_post_text


def test_meme_preset_exists():
    preset = get_preset("meme_news")
    assert preset is not None
    assert preset.profile["format_mode"] == "markdown_v2"
    assert any("meme_news" in line for line in list_preset_lines())


def test_markdown_v2_supports_quote_strike_spoiler():
    out = markdown_to_telegram_v2("**Title**\n\n> quote here\n\n~~joke~~ and ||secret||")
    assert "*Title*" in out
    assert ">quote here" in out
    assert "~joke~" in out
    assert "||secret||" in out


def test_markdownish_html_supports_quote_and_spoiler():
    html = sanitize_telegram_html(markdownish_to_html("**Title**\n> quote\n||secret||"))
    assert "<b>Title</b>" in html
    assert "<blockquote>quote</blockquote>" in html
    assert "<tg-spoiler>secret</tg-spoiler>" in html


def test_editorial_review_flags_missing_source():
    result = review_post_text("Short post", require_source=True)
    assert result.score < 100
    assert result.warnings


def test_markdownish_html_supports_markdown_links():
    html = sanitize_telegram_html(markdownish_to_html("Подписаться на [TG Media Lab](https://t.me/TMediaLabG)"))
    assert '<a href="https://t.me/TMediaLabG">TG Media Lab</a>' in html
