from app.config import Settings


def test_railway_postgres_url_is_converted_to_asyncpg():
    settings = Settings(TELEGRAM_BOT_TOKEN="x", DATABASE_URL="postgresql://u:p@host:5432/db")
    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"


def test_legacy_postgres_url_is_converted_to_asyncpg():
    settings = Settings(TELEGRAM_BOT_TOKEN="x", DATABASE_URL="postgres://u:p@host:5432/db")
    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"


def test_asyncpg_url_is_kept():
    settings = Settings(TELEGRAM_BOT_TOKEN="x", DATABASE_URL="postgresql+asyncpg://u:p@host:5432/db")
    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"


def test_railway_json_does_not_force_network_healthcheck():
    import json
    from pathlib import Path

    data = json.loads(Path("railway.json").read_text(encoding="utf-8"))
    deploy = data.get("deploy", {})
    assert "healthcheckPath" not in deploy
    assert deploy.get("startCommand") == "python -m app.main"


def test_missing_database_url_on_railway_fails_fast():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        Settings(TELEGRAM_BOT_TOKEN="x", RAILWAY_ENVIRONMENT="production", DATABASE_URL="")
    assert "DATABASE_URL is not set for Railway service" in str(exc.value)


def test_database_private_url_fallback_is_used():
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="",
        DATABASE_PRIVATE_URL="postgresql://u:p@postgres.railway.internal:5432/db",
    )
    assert settings.database_url == "postgresql+asyncpg://u:p@postgres.railway.internal:5432/db"
