from types import SimpleNamespace

from app.services.anti_cringe import sanitize_post_text, detect_ai_smells
from app.services.content_day import build_day_plan
from app.services.media_relevance import choose_safe_article_media, media_relevance
from app.services.audit import sanitize_payload
from app.services.style_presets import get_preset


def test_anti_cringe_rewrites_common_ai_phrase():
    text = "В мире видеоигр нарастает ажиотаж вокруг GTA 6. Время покажет."
    result = sanitize_post_text(text)
    assert result.changed
    assert "нарастает ажиотаж" not in result.text.lower()
    assert "время покажет" not in result.text.lower()


def test_media_relevance_rejects_logo_placeholder():
    article = SimpleNamespace(
        title="Rockstar показала трейлер GTA 6",
        summary="Новая информация о GTA 6",
        url="https://example.com/gta6-trailer",
        media_url="https://cdn.example.com/assets/logo.png",
        media_type="photo",
    )
    url, media_type, decision = choose_safe_article_media(article)
    assert url is None
    assert decision.attach is False


def test_media_relevance_accepts_same_domain_and_title_tokens():
    article = SimpleNamespace(
        title="Valday 45 PRO представлен на COMvex",
        summary="Грузовик Валдай 45 PRO показали на выставке",
        url="https://ixbt.com/live/car/valday-45-pro.html",
        media_url="https://ixbt.com/live/car/images/valday-45-pro-comvex.jpg",
        media_type="photo",
    )
    decision = media_relevance(article)
    assert decision.attach is True
    assert decision.score >= 55


def test_content_day_plan_has_no_autopublish():
    plan = build_day_plan("@client_news", "classic_news")
    assert "Публикация не выполняется автоматически" in plan
    assert "@client_news" in plan


def test_audit_sanitizes_secrets():
    payload = sanitize_payload({"AI_API_KEY": "secret", "nested": {"token": "abc"}, "ok": "value"})
    assert "secret" not in payload
    assert "abc" not in payload
    assert "***" in payload


def test_new_presets_exist():
    assert get_preset("serious_media") is not None
    assert get_preset("gaming") is not None
    assert get_preset("author_blog") is not None
