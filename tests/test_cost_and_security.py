from app.utils.url_safety import is_safe_public_url
from app.services.ai_cost import estimate_tokens_from_text, canonical_prompt


def test_private_urls_are_blocked():
    ok, reason = is_safe_public_url("http://127.0.0.1:5432/feed.xml")
    assert not ok
    assert "private" in reason or "localhost" in reason


def test_public_https_url_allowed():
    ok, _ = is_safe_public_url("https://example.com/feed.xml")
    assert ok


def test_token_estimate_and_prompt_hash_are_stable_enough():
    prompt = [{"role": "user", "content": "hello"}]
    text = canonical_prompt(prompt)
    assert estimate_tokens_from_text(text) >= 1
    assert canonical_prompt(prompt) == text
