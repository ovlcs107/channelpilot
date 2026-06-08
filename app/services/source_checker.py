from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.config import Settings
from app.services.news_collector import Article


_WORD_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")

_TOPIC_GROUPS: dict[str, set[str]] = {
    "telegram": {"telegram", "телеграм", "телега", "tg", "тг", "channel", "channels", "канал", "каналы", "каналов", "бот", "боты"},
    "ai": {"ai", "ии", "нейро", "нейросеть", "нейросети", "нейросетей", "openai", "chatgpt", "gpt", "gemini", "llm", "claude", "midjourney"},
    "smm_media": {"smm", "смм", "медиа", "media", "маркетинг", "marketing", "контент", "пост", "посты", "постинг", "реклама", "ads", "автоматизация", "автоматизацию", "автоматизации"},
    "tech": {"tech", "technology", "технологии", "технология", "технологий", "it", "айти", "digital", "софт", "стартап", "стартапы", "сервис", "приложение"},
    "gaming": {"игры", "игра", "игровой", "gaming", "game", "games", "steam", "xbox", "playstation", "gta", "minecraft"},
    "business": {"бизнес", "рынок", "предприниматель", "компания", "компании", "экономика", "деньги", "продажи", "выручка"},
}

# If a channel/request is about Telegram, AI, SMM, media or tech, general politics
# should not win only because the source is fresh/trusted. This is exactly what
# caused irrelevant Lavrov/EU drafts for a Telegram/AI media channel.
_POLITICAL_NOISE = {
    "лавров", "мид", "дипломат", "дипломатия", "министр", "президент", "путин", "трамп", "байден",
    "ес", "евросоюз", "сша", "нато", "санкции", "война", "конфликт", "политика", "политический",
    "госдума", "кремль", "украина", "россия", "иран", "израиль", "хамас", "переговоры", "посол",
}
_GENERIC_QUERY_WORDS = {
    "новость", "новости", "актуальные", "интересные", "сильную", "сильная", "свежие", "свежую",
    "сделай", "найди", "подготовь", "дайджест", "пост", "материал", "про", "для", "and", "the", "about",
}


@dataclass(slots=True)
class ArticleScore:
    article: Article
    score: int
    reasons: list[str] = field(default_factory=list)
    topic_score: int = 0


@dataclass(slots=True)
class SourceCheckResult:
    accepted: list[Article]
    rejected_count: int
    confidence: str
    risk_notes: str
    scores: list[ArticleScore] = field(default_factory=list)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def top_score(self) -> int:
        return self.scores[0].score if self.scores else 0

    def score_payload(self) -> dict[str, object]:
        return {
            "top_score": self.top_score,
            "rejected_by_reason": self.rejection_reasons,
            "scores": [
                {
                    "title": item.article.title,
                    "domain": item.article.domain,
                    "score": item.score,
                    "topic_score": item.topic_score,
                    "reasons": item.reasons,
                }
                for item in self.scores[:5]
            ],
        }


class SourceChecker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(
        self,
        articles: list[Article],
        min_sources: int | None = None,
        *,
        blocked_domains: Iterable[str] | None = None,
        trusted_domains: Iterable[str] | None = None,
        priority_domains: Iterable[str] | None = None,
        suspicious_domains: Iterable[str] | None = None,
        query: str = "",
        channel_topic: str = "",
        editorial_prompt: str = "",
    ) -> SourceCheckResult:
        min_sources = min_sources or self.settings.default_min_sources
        now = datetime.now(timezone.utc)
        max_age = timedelta(hours=self.settings.max_article_age_hours)
        blocked = {normalize_domain(d) for d in blocked_domains or []}
        trusted = {normalize_domain(d) for d in trusted_domains or []}
        priority = {normalize_domain(d) for d in priority_domains or []}
        suspicious = {normalize_domain(d) for d in suspicious_domains or []}
        context = _build_relevance_context(query=query, channel_topic=channel_topic, editorial_prompt=editorial_prompt)

        scored: list[ArticleScore] = []
        rejected = 0
        rejection_reasons: dict[str, int] = {}
        seen_topics: set[str] = set()

        def reject(reason: str) -> None:
            nonlocal rejected
            rejected += 1
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        for article in articles:
            domain = normalize_domain(article.domain)
            if domain in blocked:
                reject("домен заблокирован")
                continue
            if article.published_at and now - article.published_at.astimezone(timezone.utc) > max_age:
                reject("устарело")
                continue
            if article.topic_key in seen_topics:
                reject("дубль")
                continue

            topic_score, topic_reasons = _topic_relevance(article, context)
            min_topic_score = _min_topic_score(self.settings, context)
            if topic_score < min_topic_score:
                reject("не по теме запроса/канала")
                continue

            seen_topics.add(article.topic_key)
            scored.append(self._score_article(article, now, trusted, priority, suspicious, topic_score, topic_reasons))

        scored.sort(key=lambda item: item.score, reverse=True)
        accepted = [item.article for item in scored]
        domains = {article.domain for article in accepted if article.domain}
        top_score = scored[0].score if scored else 0

        if len(accepted) >= max(min_sources, 2) and len(domains) >= 2 and top_score >= 70:
            confidence = "high"
            risk_notes = "Есть несколько свежих источников с разных доменов, рейтинг новости высокий. Можно публиковать аккуратно."
        elif len(accepted) >= 1 and top_score >= 55:
            confidence = "medium" if min_sources <= 1 or len(accepted) >= min_sources else "low"
            risk_notes = "Источник найден, но подтверждений или рейтинга мало. Для важных тем лучше ручная проверка."
        elif accepted:
            confidence = "low"
            risk_notes = "Новость выглядит слабой по рейтингу. Лучше отправить на ручную проверку, не автопостить."
        else:
            confidence = "none"
            if rejection_reasons.get("не по теме запроса/канала"):
                risk_notes = "Материалы нашлись, но не соответствуют теме запроса или канала. Новостной пост создавать нельзя."
            else:
                risk_notes = "Не найдено свежих разрешённых источников. Новостной пост создавать нельзя."

        return SourceCheckResult(
            accepted=accepted,
            rejected_count=rejected,
            confidence=confidence,
            risk_notes=risk_notes,
            scores=scored,
            rejection_reasons=rejection_reasons,
        )

    def _score_article(
        self,
        article: Article,
        now: datetime,
        trusted_domains: set[str],
        priority_domains: set[str],
        suspicious_domains: set[str],
        topic_score: int,
        topic_reasons: list[str],
    ) -> ArticleScore:
        score = 35
        reasons: list[str] = []
        domain = normalize_domain(article.domain)

        if article.published_at:
            age_hours = max(0.0, (now - article.published_at.astimezone(timezone.utc)).total_seconds() / 3600)
            if age_hours <= 6:
                score += 22
                reasons.append("очень свежее")
            elif age_hours <= 24:
                score += 16
                reasons.append("свежее")
            elif age_hours <= 72:
                score += 8
                reasons.append("не старое")
        else:
            score += 3
            reasons.append("дата не указана")

        if domain in trusted_domains:
            score += 14
            reasons.append("доверенный домен")
        if domain in priority_domains:
            score += 10
            reasons.append("приоритетный домен")
        if domain in suspicious_domains:
            score -= 25
            reasons.append("сомнительный домен")
        if article.media_url:
            score += 4
            reasons.append("есть медиа")
        if len(article.summary.strip()) >= 140:
            score += 5
            reasons.append("есть описание")
        if len(article.title.strip()) > 8:
            score += 3

        # Topic relevance now affects ranking, not just source quality.
        score += int(topic_score * 0.32)
        reasons.extend(topic_reasons)

        return ArticleScore(article=article, score=max(0, min(100, score)), reasons=reasons, topic_score=topic_score)


def normalize_domain(value: str) -> str:
    return value.lower().strip().removeprefix("www.")


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower().replace("ё", "е") for match in _WORD_RE.finditer(text or "")}


def _contains_group(tokens: set[str], group: str) -> bool:
    words = _TOPIC_GROUPS[group]
    for token in tokens:
        if token in words:
            return True
        # Stem-like matching for Russian long forms: автоматизация/автоматизации, нейросети/нейросетей, etc.
        if len(token) >= 6 and any(token.startswith(word[:6]) for word in words if len(word) >= 6):
            return True
    return False


@dataclass(frozen=True, slots=True)
class RelevanceContext:
    query_tokens: set[str]
    channel_tokens: set[str]
    requested_groups: set[str]
    strict_media_tech: bool
    has_specific_query: bool


def _build_relevance_context(query: str, channel_topic: str, editorial_prompt: str) -> RelevanceContext:
    query_tokens = _tokens(query) - _GENERIC_QUERY_WORDS
    channel_tokens = _tokens(f"{channel_topic} {editorial_prompt}") - _GENERIC_QUERY_WORDS
    all_context = query_tokens | channel_tokens
    requested_groups = {group for group in _TOPIC_GROUPS if _contains_group(all_context, group)}
    strict_media_tech = bool(requested_groups & {"telegram", "ai", "smm_media", "tech"})
    has_specific_query = bool(query_tokens or requested_groups)
    return RelevanceContext(
        query_tokens=query_tokens,
        channel_tokens=channel_tokens,
        requested_groups=requested_groups,
        strict_media_tech=strict_media_tech,
        has_specific_query=has_specific_query,
    )


def _min_topic_score(settings: Settings, context: RelevanceContext) -> int:
    if not getattr(settings, "topic_relevance_enabled", True):
        return 0
    base = int(getattr(settings, "topic_relevance_min_score", 35))
    if not context.has_specific_query:
        return 0
    if context.strict_media_tech:
        return max(base, int(getattr(settings, "topic_relevance_strict_min_score", 45)))
    return base


def _topic_relevance(article: Article, context: RelevanceContext) -> tuple[int, list[str]]:
    if not context.has_specific_query:
        return 70, ["нет узкого запроса"]

    text = f"{article.title} {article.summary} {article.source_name} {article.domain}"
    article_tokens = _tokens(text)
    reasons: list[str] = []
    score = 0

    # Match important topic groups from the request/channel profile.
    matched_groups = []
    for group in sorted(context.requested_groups):
        if _contains_group(article_tokens, group):
            matched_groups.append(group)
            score += 24
    if matched_groups:
        reasons.append("релевантные темы: " + ", ".join(matched_groups))

    # Match concrete query words. We intentionally exclude generic words so a
    # political article cannot pass because it contains something like “медиа”.
    query_keywords = {t for t in context.query_tokens if len(t) >= 4 and t not in _GENERIC_QUERY_WORDS}
    matched_keywords = set()
    for token in query_keywords:
        if token in article_tokens:
            matched_keywords.add(token)
        elif len(token) >= 6 and any(a.startswith(token[:6]) or token.startswith(a[:6]) for a in article_tokens if len(a) >= 6):
            matched_keywords.add(token)
    if matched_keywords:
        score += min(28, 7 * len(matched_keywords))
        reasons.append("ключи запроса: " + ", ".join(sorted(matched_keywords)[:6]))

    # Channel profile may add softer relevance when query is short.
    channel_hits = 0
    for token in context.channel_tokens:
        if len(token) >= 5 and token in article_tokens:
            channel_hits += 1
    if channel_hits:
        score += min(12, channel_hits * 2)
        reasons.append("совпадает с профилем канала")

    political_noise = bool(article_tokens & _POLITICAL_NOISE)
    if context.strict_media_tech and political_noise and not (context.requested_groups & {"telegram", "ai", "smm_media", "tech"} and matched_groups):
        score -= 35
        reasons.append("политический шум вне темы канала")
    elif context.strict_media_tech and political_noise and score < 60:
        score -= 18
        reasons.append("политический шум")

    if score <= 0:
        reasons.append("нет совпадений с темой запроса")
    return max(0, min(100, score)), reasons
