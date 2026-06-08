from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False, future=True, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    from app import models  # noqa: F401 - required to register models

    last_error: Exception | None = None
    for attempt in range(1, settings.database_init_retries + 1):
        try:
            async with engine.begin() as conn:
                await _prepare_database_for_safe_create_all(conn)
                try:
                    await conn.run_sync(Base.metadata.create_all)
                except Exception as create_exc:
                    if _is_known_duplicate_index_error(create_exc):
                        logger.warning(
                            "Ignoring known duplicate confirmation index during metadata.create_all: %s",
                            create_exc,
                        )
                    else:
                        raise
                await _run_lightweight_migrations(conn)
            return
        except Exception as exc:  # pragma: no cover - depends on external DB timing
            last_error = exc
            logger.warning(
                "Database init failed, attempt %s/%s: %s",
                attempt,
                settings.database_init_retries,
                exc,
            )
            if attempt < settings.database_init_retries:
                await asyncio.sleep(settings.database_init_retry_seconds)
    raise RuntimeError(f"Не удалось подключиться к PostgreSQL: {last_error}")



def _is_known_duplicate_index_error(exc: Exception) -> bool:
    """Return true for the legacy duplicate index crash from older builds.

    Some previous archives registered ix_confirmation_user_action through
    SQLAlchemy metadata while migrations also created it manually. PostgreSQL
    treats index names as relations, so CREATE INDEX can fail with
    DuplicateTableError when the index already exists. This guard lets a
    database upgraded from an older build continue booting instead of entering
    a Railway crash loop.
    """
    text_value = str(exc)
    return "ix_confirmation_user_action" in text_value and "DuplicateTableError" in text_value


async def _prepare_database_for_safe_create_all(conn) -> None:
    """Make startup idempotent for databases touched by older builds."""
    if conn.dialect.name != "postgresql":
        return
    # Older builds could leave this index in a state where SQLAlchemy attempts
    # to create it again without IF NOT EXISTS. Dropping it before metadata
    # initialization is safe: the lightweight migration below recreates it with
    # CREATE INDEX IF NOT EXISTS.
    await conn.execute(text("DROP INDEX IF EXISTS ix_confirmation_user_action"))

async def _run_lightweight_migrations(conn) -> None:
    """Small safe upgrades for users who already started v1/v2.

    This is not a replacement for Alembic, but it prevents crashes when a fresh
    v3 archive is started against a database from the previous MVP.
    """
    if conn.dialect.name != "postgresql":
        return
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(32) DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS accepted_terms_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT false",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_reason TEXT DEFAULT ''",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS style_profile_json TEXT DEFAULT '{}'",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS editorial_prompt TEXT DEFAULT ''",
        "ALTER TABLE sources ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS primary_article_topic_key VARCHAR(128)",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS quality_score INTEGER DEFAULT 0",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS score_details_json TEXT DEFAULT '{}'",
        "ALTER TABLE published_posts ADD COLUMN IF NOT EXISTS topic_key VARCHAR(128)",
        "ALTER TABLE published_posts ADD COLUMN IF NOT EXISTS views_count INTEGER DEFAULT 0",
        "ALTER TABLE published_posts ADD COLUMN IF NOT EXISTS reactions_count INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_published_posts_channel_topic ON published_posts (channel_id, topic_key)",
        "CREATE TABLE IF NOT EXISTS ai_response_cache (id SERIAL PRIMARY KEY, prompt_hash VARCHAR(128) UNIQUE, purpose VARCHAR(64), model VARCHAR(128), response_text TEXT NOT NULL, input_tokens_est INTEGER DEFAULT 0, output_tokens_est INTEGER DEFAULT 0, hits INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT now(), last_used_at TIMESTAMP DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_ai_response_cache_prompt_hash ON ai_response_cache (prompt_hash)",
        "CREATE INDEX IF NOT EXISTS ix_ai_response_cache_created_at ON ai_response_cache (created_at)",
        "CREATE TABLE IF NOT EXISTS ai_usage_events (id SERIAL PRIMARY KEY, owner_telegram_id BIGINT DEFAULT 0, purpose VARCHAR(64), model VARCHAR(128), prompt_tokens_est INTEGER DEFAULT 0, completion_tokens_est INTEGER DEFAULT 0, cached BOOLEAN DEFAULT false, created_at TIMESTAMP DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_ai_usage_owner_created ON ai_usage_events (owner_telegram_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_ai_usage_purpose_created ON ai_usage_events (purpose, created_at)",
        "CREATE TABLE IF NOT EXISTS security_events (id SERIAL PRIMARY KEY, owner_telegram_id BIGINT DEFAULT 0, event_type VARCHAR(64), details TEXT DEFAULT '', created_at TIMESTAMP DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_security_events_owner_telegram_id ON security_events (owner_telegram_id)",
        "CREATE INDEX IF NOT EXISTS ix_security_events_event_type ON security_events (event_type)",
        "CREATE TABLE IF NOT EXISTS audit_events (id SERIAL PRIMARY KEY, user_id BIGINT DEFAULT 0, role VARCHAR(32) DEFAULT 'unknown', channel_id INTEGER, channel_username VARCHAR(255), action_type VARCHAR(64), action_payload_json TEXT DEFAULT '{}', result VARCHAR(32) DEFAULT 'success', reason TEXT DEFAULT '', related_draft_id INTEGER, related_message_id BIGINT, created_at TIMESTAMP DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_user_created ON audit_events (user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_channel_created ON audit_events (channel_username, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_action_created ON audit_events (action_type, created_at)",
        "CREATE TABLE IF NOT EXISTS confirmation_tokens (id SERIAL PRIMARY KEY, user_id BIGINT, action_type VARCHAR(64), channel_id INTEGER, draft_id INTEGER, payload_hash VARCHAR(128) DEFAULT '', expires_at TIMESTAMP, confirmed BOOLEAN DEFAULT false, created_at TIMESTAMP DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_confirmation_user_action ON confirmation_tokens (user_id, action_type)",
    ]
    for statement in statements:
        await conn.execute(text(statement))
