from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="user")  # user | admin
    accepted_terms_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    channels: Mapped[list["Channel"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    partner_deals: Mapped[list["PartnerDeal"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("owner_id", "username", name="uq_owner_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    topic: Mapped[str] = mapped_column(Text, default="новости и полезный контент")
    style: Mapped[str] = mapped_column(Text, default="коротко, понятно, без кликбейта")
    style_profile_json: Mapped[str] = mapped_column(Text, default="{}")
    editorial_prompt: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="safe")  # safe | semi | auto
    autopilot_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_reply_updates: Mapped[bool] = mapped_column(Boolean, default=True)
    media_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_times: Mapped[str] = mapped_column(String(255), default="09:00,14:00,19:00")
    min_sources: Mapped[int] = mapped_column(Integer, default=2)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    owner: Mapped[User] = relationship(back_populates="channels")
    sources: Mapped[list["Source"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    rules: Mapped[list["SourceRule"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("channel_id", "url", name="uq_channel_source_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    channel: Mapped[Channel] = relationship(back_populates="sources")


class SourceRule(Base):
    __tablename__ = "source_rules"
    __table_args__ = (UniqueConstraint("channel_id", "domain", "rule_type", name="uq_channel_domain_rule"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    rule_type: Mapped[str] = mapped_column(String(32), index=True)  # allow | block | trusted | priority | suspicious
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    channel: Mapped[Channel] = relationship(back_populates="rules")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="news")  # news | update | ad | network | prompt
    text: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    media_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # photo | video
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    primary_article_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    primary_article_topic_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    primary_article_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_article_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_article_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    quality_score: Mapped[int] = mapped_column(Integer, default=0)
    score_details_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft | published | scheduled | deleted
    confidence: Mapped[str] = mapped_column(String(32), default="low")
    risk_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    channel: Mapped[Channel] = relationship(back_populates="drafts")


class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending | published | failed | cancelled
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutopilotRun(Base):
    __tablename__ = "autopilot_runs"
    __table_args__ = (UniqueConstraint("channel_id", "slot_key", name="uq_channel_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    slot_key: Mapped[str] = mapped_column(String(64), index=True)  # YYYY-MM-DD HH:MM
    status: Mapped[str] = mapped_column(String(32), default="done")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PublishedPost(Base):
    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    draft_id: Mapped[Optional[int]] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32), default="news")
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    article_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    topic_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    article_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    article_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    media_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    views_count: Mapped[int] = mapped_column(Integer, default=0)
    reactions_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (UniqueConstraint("url_hash", name="uq_media_url_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(128), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(16))
    local_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PartnerDeal(Base):
    __tablename__ = "partner_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[Optional[int]] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"), nullable=True, index=True)
    partner_channel: Mapped[str] = mapped_column(String(255), index=True)
    terms: Mapped[str] = mapped_column(Text)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | done | cancelled
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    owner: Mapped[User] = relationship(back_populates="partner_deals")

class AIResponseCache(Base):
    __tablename__ = "ai_response_cache"
    __table_args__ = (UniqueConstraint("prompt_hash", name="uq_ai_response_prompt_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_hash: Mapped[str] = mapped_column(String(128), index=True)
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    response_text: Mapped[str] = mapped_column(Text)
    input_tokens_est: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens_est: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AIUsageEvent(Base):
    __tablename__ = "ai_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0)
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens_est: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens_est: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0)
    role: Mapped[str] = mapped_column(String(32), default="unknown")
    channel_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    channel_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    action_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[str] = mapped_column(String(32), default="success")
    reason: Mapped[str] = mapped_column(Text, default="")
    related_draft_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    related_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ConfirmationToken(Base):
    __tablename__ = "confirmation_tokens"
    # Composite index is created in app.database with CREATE INDEX IF NOT EXISTS.

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    draft_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    payload_hash: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
