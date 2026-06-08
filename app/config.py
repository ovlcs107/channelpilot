from __future__ import annotations

from functools import lru_cache
import json
from typing import List, Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode


LOCAL_DATABASE_URL = "postgresql+asyncpg://channelpilot:channelpilot@localhost:5432/channelpilot"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    database_url: str = Field(default="", alias="DATABASE_URL")
    # Railway can expose several Postgres URLs depending on the database plugin/version.
    # If DATABASE_URL is not mapped in the bot service, these fallbacks let the app use
    # DATABASE_PRIVATE_URL or DATABASE_PUBLIC_URL when the user maps one of them instead.
    database_private_url: str = Field(default="", alias="DATABASE_PRIVATE_URL")
    database_public_url: str = Field(default="", alias="DATABASE_PUBLIC_URL")
    railway_environment: str = Field(default="", alias="RAILWAY_ENVIRONMENT")
    railway_project_id: str = Field(default="", alias="RAILWAY_PROJECT_ID")
    railway_service_name: str = Field(default="", alias="RAILWAY_SERVICE_NAME")
    port: int = Field(default=8080, alias="PORT")
    health_host: str = Field(default="0.0.0.0", alias="HEALTH_HOST")
    health_server_enabled: bool = Field(default=True, alias="HEALTH_SERVER_ENABLED")
    database_init_retries: int = Field(default=40, alias="DATABASE_INIT_RETRIES")
    database_init_retry_seconds: float = Field(default=2.0, alias="DATABASE_INIT_RETRY_SECONDS")

    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    ai_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="AI_BASE_URL")
    ai_model: str = Field(default="openai/gpt-4o-mini", alias="AI_MODEL")
    ai_temperature: float = Field(default=0.28, alias="AI_TEMPERATURE")
    ai_site_url: str = Field(default="https://example.com", alias="AI_SITE_URL")
    ai_app_name: str = Field(default="ChannelPilot Pro", alias="AI_APP_NAME")
    project_edition: str = Field(default="private_commercial", alias="PROJECT_EDITION")
    project_name: str = Field(default="Professional Channel Network", alias="PROJECT_NAME")
    support_contact: str = Field(default="", alias="SUPPORT_CONTACT")
    ai_fast_model: str = Field(default="openai/gpt-4o-mini", alias="AI_FAST_MODEL")
    ai_request_timeout: float = Field(default=25.0, alias="AI_REQUEST_TIMEOUT")

    # Cost control: compact prompts, cache repeated generations, and stop runaway spend.
    cost_saver_enabled: bool = Field(default=True, alias="COST_SAVER_ENABLED")
    cost_saver_mode: str = Field(default="cheap", alias="COST_SAVER_MODE")  # cheap | balanced | quality
    ai_cache_enabled: bool = Field(default=True, alias="AI_CACHE_ENABLED")
    ai_cache_ttl_hours: int = Field(default=168, alias="AI_CACHE_TTL_HOURS")
    ai_daily_token_budget: int = Field(default=50_000, alias="AI_DAILY_TOKEN_BUDGET")
    ai_monthly_token_budget: int = Field(default=1_000_000, alias="AI_MONTHLY_TOKEN_BUDGET")
    ai_prompt_max_articles: int = Field(default=4, alias="AI_PROMPT_MAX_ARTICLES")
    ai_source_summary_chars: int = Field(default=420, alias="AI_SOURCE_SUMMARY_CHARS")
    ai_max_input_chars: int = Field(default=12_000, alias="AI_MAX_INPUT_CHARS")
    ai_max_output_tokens: int = Field(default=800, alias="AI_MAX_OUTPUT_TOKENS")
    editorial_prompt_max_chars: int = Field(default=6000, alias="EDITORIAL_PROMPT_MAX_CHARS")
    prompt_post_max_channels: int = Field(default=5, alias="PROMPT_POST_MAX_CHANNELS")

    # v12 natural language control: user writes normal text, router maps it to safe actions.
    natural_control_enabled: bool = Field(default=True, alias="NATURAL_CONTROL_ENABLED")
    natural_control_use_ai: bool = Field(default=True, alias="NATURAL_CONTROL_USE_AI")
    natural_control_require_confirm_for_publish: bool = Field(default=True, alias="NATURAL_CONTROL_REQUIRE_CONFIRM_FOR_PUBLISH")
    dm_parse_mode: str = Field(default="markdown_v2", alias="BOT_DM_PARSE_MODE")

    # Access/security. This build is made for private/commercial deployments with explicit admins and allowed channels.
    bot_access_mode: str = Field(default="private", alias="BOT_ACCESS_MODE")
    require_admin_ids: bool = Field(default=True, alias="REQUIRE_ADMIN_IDS")
    allowed_channels: Annotated[List[str], NoDecode] = Field(default_factory=list, alias="ALLOWED_CHANNELS")
    require_channel_verification: bool = Field(default=True, alias="REQUIRE_CHANNEL_VERIFICATION")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_window_seconds: int = Field(default=60, alias="RATE_LIMIT_WINDOW_SECONDS")
    max_commands_per_window: int = Field(default=24, alias="MAX_COMMANDS_PER_WINDOW")
    source_url_private_networks_blocked: bool = Field(default=True, alias="SOURCE_URL_PRIVATE_NETWORKS_BLOCKED")

    # Public multi-tenant safety. These limits prevent one public user from
    # hijacking channels, draining AI budget, or creating a mess in queues.
    public_require_terms_acceptance: bool = Field(default=False, alias="PUBLIC_REQUIRE_TERMS_ACCEPTANCE")
    public_require_user_channel_admin: bool = Field(default=True, alias="PUBLIC_REQUIRE_USER_CHANNEL_ADMIN")
    public_double_confirm_publish: bool = Field(default=True, alias="PUBLIC_DOUBLE_CONFIRM_PUBLISH")
    public_allow_auto_publish: bool = Field(default=False, alias="PUBLIC_ALLOW_AUTO_PUBLISH")
    public_max_channels_per_user: int = Field(default=5, alias="PUBLIC_MAX_CHANNELS_PER_USER")
    public_max_sources_per_channel: int = Field(default=40, alias="PUBLIC_MAX_SOURCES_PER_CHANNEL")
    public_max_drafts_per_day: int = Field(default=25, alias="PUBLIC_MAX_DRAFTS_PER_DAY")
    public_max_scheduled_per_channel: int = Field(default=30, alias="PUBLIC_MAX_SCHEDULED_PER_CHANNEL")
    public_max_network_channels_per_request: int = Field(default=5, alias="PUBLIC_MAX_NETWORK_CHANNELS_PER_REQUEST")

    newsapi_key: str = Field(default="", alias="NEWSAPI_KEY")
    newsapi_languages: str = Field(default="ru,en", alias="NEWSAPI_LANGUAGES")
    brave_search_api_key: str = Field(default="", alias="BRAVE_SEARCH_API_KEY")

    max_article_age_hours: int = Field(default=72, alias="MAX_ARTICLE_AGE_HOURS")
    default_min_sources: int = Field(default=2, alias="DEFAULT_MIN_SOURCES")
    max_articles_per_request: int = Field(default=8, alias="MAX_ARTICLES_PER_REQUEST")
    rss_cache_ttl_seconds: int = Field(default=300, alias="RSS_CACHE_TTL_SECONDS")
    search_cache_ttl_seconds: int = Field(default=600, alias="SEARCH_CACHE_TTL_SECONDS")
    duplicate_topic_window_days: int = Field(default=14, alias="DUPLICATE_TOPIC_WINDOW_DAYS")
    autopilot_min_score: int = Field(default=65, alias="AUTOPILOT_MIN_SCORE")
    autopilot_slot_window_minutes: int = Field(default=5, alias="AUTOPILOT_SLOT_WINDOW_MINUTES")
    topic_relevance_enabled: bool = Field(default=True, alias="TOPIC_RELEVANCE_ENABLED")
    topic_relevance_min_score: int = Field(default=35, alias="TOPIC_RELEVANCE_MIN_SCORE")
    topic_relevance_strict_min_score: int = Field(default=45, alias="TOPIC_RELEVANCE_STRICT_MIN_SCORE")

    extract_article_media: bool = Field(default=True, alias="EXTRACT_ARTICLE_MEDIA")
    article_media_fetch_limit: int = Field(default=6, alias="ARTICLE_MEDIA_FETCH_LIMIT")
    media_enabled_by_default: bool = Field(default=True, alias="MEDIA_ENABLED_BY_DEFAULT")
    telegram_caption_limit: int = Field(default=1024, alias="TELEGRAM_CAPTION_LIMIT")
    post_format_default: str = Field(default="html", alias="POST_FORMAT_DEFAULT")  # html | markdown_v2 | plain
    post_link_preview_enabled: bool = Field(default=True, alias="POST_LINK_PREVIEW_ENABLED")

    # Public channel footer. For media-looking posts we keep source URLs internal
    # and publish only a clean subscription CTA by default. Source URLs are still
    # stored in draft.sources_json / PublishedPost for audit and safety.
    source_footer_mode: str = Field(default="brand_cta", alias="SOURCE_FOOTER_MODE")  # brand_cta | hidden | sources
    post_source_debug_enabled: bool = Field(default=False, alias="POST_SOURCE_DEBUG_ENABLED")
    brand_cta_enabled: bool = Field(default=True, alias="BRAND_CTA_ENABLED")
    brand_cta_prefix: str = Field(default="Подписаться на", alias="BRAND_CTA_PREFIX")
    brand_cta_label: str = Field(default="TG Media Lab", alias="BRAND_CTA_LABEL")
    brand_cta_url: str = Field(default="https://t.me/TMediaLabG", alias="BRAND_CTA_URL")

    media_download_enabled: bool = Field(default=True, alias="MEDIA_DOWNLOAD_ENABLED")
    media_storage_dir: str = Field(default="data/media", alias="MEDIA_STORAGE_DIR")
    media_max_bytes: int = Field(default=15_000_000, alias="MEDIA_MAX_BYTES")


    # v20 production guardrails: roles, demo limits, audit, safer media and model routing.
    ai_router_model: str = Field(default="", alias="AI_ROUTER_MODEL")
    ai_draft_model: str = Field(default="", alias="AI_DRAFT_MODEL")
    ai_rewrite_model: str = Field(default="", alias="AI_REWRITE_MODEL")
    ai_review_model: str = Field(default="", alias="AI_REVIEW_MODEL")
    ai_quality_model: str = Field(default="", alias="AI_QUALITY_MODEL")
    ai_default_model: str = Field(default="", alias="AI_DEFAULT_MODEL")

    demo_mode: bool = Field(default=False, alias="DEMO_MODE")
    demo_expires_days: int = Field(default=7, alias="DEMO_EXPIRES_DAYS")
    demo_max_posts_per_day: int = Field(default=5, alias="DEMO_MAX_POSTS_PER_DAY")
    demo_max_channels: int = Field(default=1, alias="DEMO_MAX_CHANNELS")
    demo_require_confirmation: bool = Field(default=True, alias="DEMO_REQUIRE_CONFIRMATION")

    max_ai_requests_per_day: int = Field(default=300, alias="MAX_AI_REQUESTS_PER_DAY")
    max_newsapi_requests_per_day: int = Field(default=80, alias="NEWSAPI_DAILY_LIMIT")
    max_digests_per_day: int = Field(default=10, alias="MAX_DIGESTS_PER_DAY")
    max_news_per_channel_per_day: int = Field(default=30, alias="MAX_NEWS_PER_CHANNEL_PER_DAY")
    max_natural_requests_per_day: int = Field(default=200, alias="MAX_NATURAL_REQUESTS_PER_DAY")
    max_prompt_length: int = Field(default=6000, alias="MAX_PROMPT_LENGTH")

    media_relevance_enabled: bool = Field(default=True, alias="MEDIA_RELEVANCE_ENABLED")
    media_relevance_min_score: int = Field(default=55, alias="MEDIA_RELEVANCE_MIN_SCORE")
    anti_cringe_enabled: bool = Field(default=True, alias="ANTI_CRINGE_ENABLED")
    audit_enabled: bool = Field(default=True, alias="AUDIT_ENABLED")
    content_day_enabled: bool = Field(default=True, alias="CONTENT_DAY_ENABLED")

    app_timezone: str = Field(default="Europe/Amsterdam", alias="APP_TIMEZONE")
    admin_ids: Annotated[List[int], NoDecode] = Field(default_factory=list, alias="ADMIN_IDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("database_url", "database_private_url", "database_public_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> str:
        """Railway usually exposes postgres:// or postgresql:// URLs.

        SQLAlchemy async engines need the asyncpg driver in the scheme. Keeping
        this here lets Railway users paste/reference DATABASE_URL directly.
        """
        if value is None:
            return ""
        url = str(value).strip()
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

    @model_validator(mode="after")
    def pick_database_url(self) -> "Settings":
        if not self.database_url:
            self.database_url = self.database_private_url or self.database_public_url

        is_railway = bool(
            self.railway_environment or self.railway_project_id or self.railway_service_name
        )
        if not self.database_url:
            if is_railway:
                raise ValueError(
                    "DATABASE_URL is not set for Railway service. "
                    "Create a PostgreSQL service and add DATABASE_URL=${{Postgres.DATABASE_URL}} "
                    "in the bot service Variables tab. If your database service has another name, "
                    "use that name instead of Postgres."
                )
            self.database_url = LOCAL_DATABASE_URL
        return self

    @staticmethod
    def _parse_env_list(value: object) -> list[object]:
        """Accept Railway-friendly comma lists and JSON arrays.

        Examples:
        ADMIN_IDS=7851246214
        ADMIN_IDS=7851246214,123
        ADMIN_IDS=[7851246214,123]
        ALLOWED_CHANNELS=@client_news,@brand_media
        ALLOWED_CHANNELS=["@client_news","@brand_media"]
        """
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        raw = str(value).strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        return [part.strip() for part in raw.split(",") if part.strip()]

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> List[int]:
        result: list[int] = []
        for item in cls._parse_env_list(value):
            raw = str(item).strip().strip('"').strip("'")
            if not raw:
                continue
            try:
                result.append(int(raw))
            except ValueError:
                continue
        return sorted(set(result))

    @field_validator("allowed_channels", mode="before")
    @classmethod
    def parse_allowed_channels(cls, value: object) -> List[str]:
        channels: List[str] = []
        for item in cls._parse_env_list(value):
            name = str(item).strip().strip('"').strip("'").lower()
            if not name:
                continue
            if not name.startswith("@"):
                name = "@" + name
            channels.append(name)
        return sorted(set(channels))

    def is_channel_allowed(self, username: str) -> bool:
        if not self.allowed_channels:
            return True
        normalized = (username or "").strip().lower()
        if normalized and not normalized.startswith("@"):
            normalized = "@" + normalized
        return normalized in self.allowed_channels

    @field_validator("cost_saver_mode")
    @classmethod
    def validate_cost_saver_mode(cls, value: str) -> str:
        value = (value or "cheap").strip().lower()
        if value not in {"cheap", "balanced", "quality"}:
            return "cheap"
        return value

    @field_validator("post_format_default")
    @classmethod
    def validate_post_format_default(cls, value: str) -> str:
        value = (value or "html").strip().lower().replace("-", "_")
        aliases = {"md": "markdown_v2", "markdown": "markdown_v2", "markdownv2": "markdown_v2", "text": "plain", "off": "plain", "none": "plain"}
        value = aliases.get(value, value)
        if value not in {"html", "markdown_v2", "plain"}:
            return "html"
        return value

    @field_validator("source_footer_mode")
    @classmethod
    def validate_source_footer_mode(cls, value: str) -> str:
        value = (value or "brand_cta").strip().lower().replace("-", "_")
        aliases = {
            "off": "hidden",
            "none": "hidden",
            "no_source": "brand_cta",
            "no_sources": "brand_cta",
            "cta": "brand_cta",
            "brand": "brand_cta",
            "source": "sources",
            "source_links": "sources",
        }
        value = aliases.get(value, value)
        if value not in {"brand_cta", "hidden", "sources"}:
            return "brand_cta"
        return value

    @field_validator("bot_access_mode")
    @classmethod
    def validate_bot_access_mode(cls, value: str) -> str:
        value = (value or "admin_only").strip().lower()
        if value not in {"admin_only", "private", "closed", "open", "public"}:
            return "admin_only"
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
