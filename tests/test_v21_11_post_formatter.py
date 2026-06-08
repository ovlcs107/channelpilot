from app.services.post_formatter import normalize_channel_post_layout
from app.services.telegram_formatting import format_for_telegram, FORMAT_HTML


def test_layout_adds_heading_and_spaces_cta():
    raw = "Портативный 4K-проектор от JMGO — новый чемпион\nJMGO выпустила портативный проектор N3 Ultimate. Устройство отлично справляется с умеренным фоновым освещением. Новинка похожа на дорогие стационарные решения. Подписаться на [TG Media Lab](https://t.me/TMediaLabG)"
    result = normalize_channel_post_layout(raw)
    assert result.startswith("**Портативный 4K-проектор от JMGO — новый чемпион**\n\n")
    assert "\n\nПодписаться на [TG Media Lab](https://t.me/TMediaLabG)" in result
    assert "Устройство отлично" in result


def test_html_conversion_keeps_clean_link_and_bold_heading():
    raw = "Xbox Games Showcase: что показали\nКонсольная индустрия ждёт анонсов.\n\nПодписаться на [TG Media Lab](https://t.me/TMediaLabG)"
    html = format_for_telegram(raw, FORMAT_HTML)
    assert "<b>Xbox Games Showcase: что показали</b>" in html
    assert '<a href="https://t.me/TMediaLabG">TG Media Lab</a>' in html
