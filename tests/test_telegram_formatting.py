from app.models import Channel
from app.services.telegram_formatting import (
    channel_format_mode,
    escape_markdown_v2,
    format_for_telegram,
    normalize_format_mode,
    parse_mode_for_format,
    sanitize_telegram_html,
)


def test_normalize_format_aliases():
    assert normalize_format_mode("markdown") == "markdown_v2"
    assert normalize_format_mode("off") == "plain"
    assert normalize_format_mode("html") == "html"
    assert parse_mode_for_format("html") == "HTML"
    assert parse_mode_for_format("markdown_v2") == "MarkdownV2"
    assert parse_mode_for_format("plain") is None


def test_sanitize_keeps_safe_telegram_html_and_removes_bad_tags():
    text = '<b>Заголовок</b><script>alert(1)</script><a href="javascript:bad">bad</a><i>ok</i>'
    result = sanitize_telegram_html(text)
    assert "<b>Заголовок</b>" in result
    assert "<script>" not in result
    assert "javascript:" not in result
    assert "<i>ok</i>" in result


def test_markdownish_to_html_output_is_safe():
    result = format_for_telegram("# Заголовок\n\n**важно** & <опасно>", "html")
    assert "<b>Заголовок</b>" in result
    assert "<b>важно</b>" in result
    assert "&lt;опасно&gt;" in result


def test_markdown_v2_escape():
    assert escape_markdown_v2("a_b [x]!") == r"a\_b \[x\]\!"


def test_channel_format_mode_from_profile():
    ch = Channel(owner_id=1, username="@x", style_profile_json='{"format_mode":"plain"}')
    assert channel_format_mode(ch, "html") == "plain"


def test_markdownish_to_html_supports_links():
    result = format_for_telegram("Подписаться на [TG Media Lab](https://t.me/TMediaLabG)", "html")
    assert '<a href="https://t.me/TMediaLabG">TG Media Lab</a>' in result
