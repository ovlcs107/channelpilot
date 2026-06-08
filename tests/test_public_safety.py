from pathlib import Path

from app.config import Settings


def test_private_commercial_defaults_are_safe():
    settings = Settings(TELEGRAM_BOT_TOKEN="x", DATABASE_URL="postgresql://u:p@host:5432/db")
    assert settings.bot_access_mode == "private"
    assert settings.require_admin_ids is True
    assert settings.public_require_terms_acceptance is False
    assert settings.public_require_user_channel_admin is True
    assert settings.public_double_confirm_publish is True
    assert settings.public_allow_auto_publish is False


def test_allowed_channels_are_normalized():
    settings = Settings(TELEGRAM_BOT_TOKEN="x", DATABASE_URL="postgresql://u:p@host:5432/db", ALLOWED_CHANNELS="Client_News,@World")
    assert settings.allowed_channels == ["@client_news", "@world"]
    assert settings.is_channel_allowed("@CLIENT_NEWS") is True
    assert settings.is_channel_allowed("other") is False


def test_public_limits_are_configurable():
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="postgresql://u:p@host:5432/db",
        PUBLIC_MAX_CHANNELS_PER_USER=3,
        PUBLIC_MAX_DRAFTS_PER_DAY=7,
    )
    assert settings.public_max_channels_per_user == 3
    assert settings.public_max_drafts_per_day == 7


def test_public_safety_code_contains_channel_admin_check():
    text = Path("app/services/publisher.py").read_text(encoding="utf-8")
    assert "verify_channel_access" in text
    assert "get_chat_administrators" in text


def test_publish_is_two_step_in_keyboard_source():
    text = Path("app/bot/keyboards.py").read_text(encoding="utf-8")
    assert "draft:confirm:" in text
    assert "draft:publish:" in text
    assert "draft:cancelpub:" in text


def test_admin_commands_are_owner_only_in_middleware_source():
    text = Path("app/bot/security.py").read_text(encoding="utf-8")
    assert "ADMIN_COMMANDS" in text
    assert "adminusers" in text
    assert "securitylog" in text
