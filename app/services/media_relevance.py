from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any

BAD_MEDIA_HINTS = {
    "logo", "favicon", "sprite", "avatar", "placeholder", "default", "banner", "button", "icon", "watermark", "adserver", "pixel"
}
GENERIC_CDN_HINTS = {"thumb", "thumbnail", "social", "socialpics", "preview", "og"}

@dataclass(slots=True)
class MediaDecision:
    attach: bool
    reason: str
    score: int


def _tokens(value: str) -> set[str]:
    return {t for t in re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", " ", (value or "").lower()).split() if len(t) >= 4}


def media_relevance(article: Any) -> MediaDecision:
    """Conservative media relevance filter.

    It cannot truly see the picture, but it prevents the most common failures:
    logos, CDN thumbnails used as article links, generic search thumbnails and
    completely unrelated assets. If in doubt, return attach=False: no photo is
    safer than a wrong photo in a public Telegram channel.
    """
    media_url = str(getattr(article, "media_url", "") or "").strip()
    media_type = str(getattr(article, "media_type", "") or "").strip()
    if not media_url:
        return MediaDecision(False, "media_url отсутствует", 0)
    parsed = urlparse(media_url)
    path = parsed.path.lower()
    host = parsed.netloc.lower().removeprefix("www.")
    filename = path.rsplit("/", 1)[-1]
    if any(h in filename for h in BAD_MEDIA_HINTS):
        return MediaDecision(False, "похоже на логотип/иконку/плейсхолдер", 5)
    if not media_type or media_type not in {"photo", "video"}:
        return MediaDecision(False, "тип медиа не подтверждён", 10)

    title = str(getattr(article, "title", "") or "")
    summary = str(getattr(article, "summary", "") or "")
    source_url = str(getattr(article, "url", "") or "")
    source_host = urlparse(source_url).netloc.lower().removeprefix("www.")
    title_tokens = _tokens(title + " " + summary)
    media_tokens = _tokens(filename.replace("_", " ").replace("-", " ") + " " + host)
    overlap = title_tokens & media_tokens

    score = 45
    if source_host and (host == source_host or host.endswith("." + source_host) or source_host.endswith("." + host)):
        score += 25
    if overlap:
        score += min(25, len(overlap) * 8)
    if any(h in path for h in GENERIC_CDN_HINTS) and not overlap:
        score -= 20
    if host.startswith(("img.", "images.", "cdn.", "media.", "static.")) and not overlap:
        score -= 10
    if score >= 55:
        return MediaDecision(True, "медиа похоже связано со статьёй", min(100, score))
    return MediaDecision(False, "медиа сомнительное, безопаснее не прикреплять", max(0, score))


def choose_safe_article_media(article: Any) -> tuple[str | None, str | None, MediaDecision]:
    decision = media_relevance(article)
    if not decision.attach:
        return None, None, decision
    return getattr(article, "media_url", None), getattr(article, "media_type", None), decision
