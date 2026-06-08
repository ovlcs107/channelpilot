from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional, TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

try:
    import feedparser  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional fallback for minimal environments
    feedparser = None

import httpx
import xml.etree.ElementTree as ET

from app.config import Settings

if TYPE_CHECKING:
    from app.models import Channel, Source
else:
    Channel = Any  # type: ignore
    Source = Any  # type: ignore
from app.utils.text import strip_html, truncate
from app.utils.url_safety import is_safe_public_url

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
TRACKING_PARAMS_PREFIXES = ("utm_",)
TRACKING_PARAMS = {"utm", "fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}
STOPWORDS = {"что", "как", "для", "или", "это", "уже", "все", "при", "the", "and", "with", "from", "about", "после", "новый", "новая", "новые", "анонс", "стало", "получила", "получил", "получили"}
MEDIA_CDN_HOST_HINTS = ("iv.", "img.", "image.", "images.", "static.", "cdn.", "media.", "thumb.", "thumbnail.")
MEDIA_PATH_HINTS = ("/socialpics/", "/social_pics/", "/thumb/", "/thumbs/", "/thumbnail/", "/thumbnails/", "/images/", "/image/", "/img/", "/photo/", "/photos/")
BAD_MEDIA_NAME_HINTS = ("logo", "favicon", "sprite", "avatar", "placeholder", "default", "banner", "button", "icon", "watermark")


def _is_probably_media_resource_url(value: str, content_type: str | None = None) -> bool:
    """Return True when a URL is likely an image/video file or CDN asset, not an article page.

    Some RSS feeds expose image CDN URLs in the item link field. If such a URL
    becomes the source link, users get broken pages like iv.kommersant.ru/... .
    Treat those as media candidates, never as article sources.
    """
    if not value:
        return False
    ct = (content_type or "").lower()
    if ct.startswith(("image/", "video/")):
        return True
    parsed = urlparse(value.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS):
        return True
    # Known image CDN pattern used by Kommersant and similar media resources.
    if host.startswith("iv.") and "kommersant" in host:
        return True
    if any(hint in path for hint in MEDIA_PATH_HINTS) and any(host.startswith(prefix) for prefix in MEDIA_CDN_HOST_HINTS):
        return True
    return False


def _is_bad_media_asset(value: str) -> bool:
    """Filter obvious logos/placeholders so previews do not attach random site graphics."""
    path = urlparse(value or "").path.lower()
    filename = path.rsplit("/", 1)[-1]
    return any(hint in filename for hint in BAD_MEDIA_NAME_HINTS)


def _is_valid_article_url(value: str) -> bool:
    if not value or not value.startswith(("http://", "https://")):
        return False
    if _is_probably_media_resource_url(value):
        return False
    ok, _reason = is_safe_public_url(value)
    return ok


def _entry_article_link(entry: Any) -> str:
    """Select an HTML article URL from feedparser entry links.

    Prefer rel=alternate/text/html links and ignore enclosure/media/CDN links.
    """
    candidates: list[tuple[int, str]] = []
    for item in entry.get("links", []) or []:
        href = str(item.get("href") or "").strip()
        if not href:
            continue
        rel = str(item.get("rel") or "").lower()
        typ = str(item.get("type") or "").lower()
        if rel == "enclosure" or _is_probably_media_resource_url(href, typ):
            continue
        score = 10
        if rel == "alternate":
            score += 20
        if "html" in typ:
            score += 15
        candidates.append((score, href))
    raw_link = str(entry.get("link") or "").strip()
    if raw_link and not _is_probably_media_resource_url(raw_link):
        candidates.append((25, raw_link))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


@dataclass(slots=True)
class Article:
    title: str
    url: str
    summary: str
    source_name: str
    published_at: Optional[datetime] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None  # photo | video
    updated_at: Optional[datetime] = None

    @property
    def domain(self) -> str:
        host = urlparse(self.url).netloc.lower()
        return host.removeprefix("www.")

    @property
    def normalized_url(self) -> str:
        return normalize_article_url(self.url)

    @property
    def article_key(self) -> str:
        raw = self.normalized_url or f"{self.domain}|{self.title.lower().strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def topic_key(self) -> str:
        text = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+", " ", self.title.lower())
        tokens = [token for token in text.split() if len(token) > 3 and token not in STOPWORDS]
        normalized = " ".join(sorted(tokens[:12])) or self.normalized_url or self.title.lower().strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        raw = "|".join(
            [
                self.title.strip().lower(),
                self.summary.strip().lower(),
                self.media_url or "",
                self.media_type or "",
                self.published_at.isoformat() if self.published_at else "",
                self.updated_at.isoformat() if self.updated_at else "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        # Backwards-compatible alias used by older tests/code.
        return self.article_key

    def as_source_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source_name": self.source_name,
            "summary": self.summary,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "media_url": self.media_url,
            "media_type": self.media_type,
            "article_key": self.article_key,
            "topic_key": self.topic_key,
            "content_hash": self.content_hash,
        }


def normalize_article_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return value.strip()
    query = []
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_PARAMS or any(key_lower.startswith(prefix) for prefix in TRACKING_PARAMS_PREFIXES):
            continue
        query.append((key, val))
    clean_path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower().removeprefix("www."), clean_path, "", urlencode(query), ""))


def _parse_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = parsedate_to_datetime(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                raw = value.replace("Z", "+00:00")
                dt = datetime.fromisoformat(raw)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return None
    return None


def _query_matches(article: Article, query: str) -> bool:
    query = query.strip().lower()
    if not query:
        return True
    text = f"{article.title} {article.summary}".lower()
    tokens = [token for token in query.replace(",", " ").split() if len(token) > 2]
    if not tokens:
        return True
    return any(token in text for token in tokens)


def _guess_media_type(media_url: str, content_type: str | None = None) -> Optional[str]:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "photo"
    if ct.startswith("video/"):
        return "video"
    path = urlparse(media_url).path.lower()
    for ext in IMAGE_EXTENSIONS:
        if path.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if path.endswith(ext):
            return "video"
    return None


def _pick_media(candidates: Iterable[tuple[str | None, str | None]]) -> tuple[Optional[str], Optional[str]]:
    photos: list[str] = []
    videos: list[str] = []
    unknowns: list[str] = []
    for raw_url, raw_type in candidates:
        if not raw_url:
            continue
        url = html.unescape(str(raw_url)).strip()
        if not url.startswith(("http://", "https://")):
            continue
        media_type = _guess_media_type(url, raw_type)
        if _is_bad_media_asset(url):
            continue
        if media_type == "photo":
            photos.append(url)
        elif media_type == "video":
            videos.append(url)
        else:
            # Unknown media often turns out to be a logo, tracking pixel, or unrelated
            # search thumbnail. Do not attach it to channel posts.
            unknowns.append(url)
    if photos:
        return photos[0], "photo"
    if videos:
        return videos[0], "video"
    return None, None


def _extract_feedparser_media(entry: Any) -> tuple[Optional[str], Optional[str]]:
    candidates: list[tuple[str | None, str | None]] = []
    for item in entry.get("media_content", []) or []:
        candidates.append((item.get("url"), item.get("type") or item.get("medium")))
    for item in entry.get("media_thumbnail", []) or []:
        candidates.append((item.get("url"), item.get("type")))
    for item in entry.get("enclosures", []) or []:
        candidates.append((item.get("href") or item.get("url"), item.get("type")))
    for item in entry.get("links", []) or []:
        if item.get("rel") == "enclosure":
            candidates.append((item.get("href"), item.get("type")))
    if entry.get("image"):
        image_value = entry.get("image")
        if isinstance(image_value, dict):
            candidates.append((image_value.get("href") or image_value.get("url"), image_value.get("type")))
        else:
            candidates.append((str(image_value), None))
    return _pick_media(candidates)


def _find_xml_text_or_attr(node: ET.Element, names: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for name in names:
        child = node.find(name)
        if child is None:
            continue
        value = child.get("url") or child.get("href") or (child.text or "")
        content_type = child.get("type") or child.get("medium")
        if value:
            return value, content_type
    return None, None


def _extract_xml_media(item: ET.Element) -> tuple[Optional[str], Optional[str]]:
    candidates: list[tuple[str | None, str | None]] = []
    for child in list(item):
        tag = child.tag.lower()
        if tag.endswith("enclosure") or tag.endswith("content") or tag.endswith("thumbnail"):
            candidates.append((child.get("url") or child.get("href") or child.text, child.get("type") or child.get("medium")))
    image_url, image_type = _find_xml_text_or_attr(item, ["image", "{http://search.yahoo.com/mrss/}thumbnail"])
    candidates.append((image_url, image_type))
    return _pick_media(candidates)



def _first_child(node: ET.Element, names: Iterable[str]) -> Optional[ET.Element]:
    for name in names:
        child = node.find(name)
        if child is not None:
            return child
    return None

def _extract_meta_media(html_text: str, page_url: str) -> tuple[Optional[str], Optional[str]]:
    patterns = [
        (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', "photo"),
        (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', "photo"),
        (r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', "photo"),
        (r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']', "video"),
        (r'<meta[^>]+property=["\']og:video:url["\'][^>]+content=["\']([^"\']+)["\']', "video"),
        (r'<meta[^>]+property=["\']og:video:secure_url["\'][^>]+content=["\']([^"\']+)["\']', "video"),
    ]
    candidates: list[tuple[str | None, str | None]] = []
    for pattern, forced_type in patterns:
        for match in re.finditer(pattern, html_text, flags=re.IGNORECASE):
            media_url = html.unescape(match.group(1).strip())
            if media_url:
                candidates.append((urljoin(page_url, media_url), f"{forced_type}/*"))
    return _pick_media(candidates)


class NewsCollector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._rss_cache: dict[str, tuple[datetime, list[Article]]] = {}
        self._search_cache: dict[str, tuple[datetime, list[Article]]] = {}

    async def collect(self, channel: Channel, sources: Iterable[Source], query: str = "") -> list[Article]:
        articles: list[Article] = []
        rss_tasks = [self._fetch_rss(source) for source in sources if source.enabled]
        if rss_tasks:
            rss_results = await asyncio.gather(*rss_tasks, return_exceptions=True)
            for result in rss_results:
                if isinstance(result, Exception):
                    logger.warning("RSS fetch failed: %s", result)
                    continue
                articles.extend(result)

        search_query = query.strip() or channel.topic
        if self.settings.newsapi_key:
            newsapi_languages = [lang.strip().lower() for lang in self.settings.newsapi_languages.split(",") if lang.strip()] or ["ru"]
            newsapi_results = await asyncio.gather(
                *(self._fetch_newsapi(search_query, language=lang) for lang in newsapi_languages),
                return_exceptions=True,
            )
            for result in newsapi_results:
                if isinstance(result, Exception):
                    logger.warning("NewsAPI language fetch failed: %s", result)
                    continue
                articles.extend(result)
        if self.settings.brave_search_api_key:
            articles.extend(await self._fetch_brave(search_query))

        articles = [a for a in articles if _is_valid_article_url(a.url)]
        articles = [a for a in articles if _query_matches(a, search_query if query else "")]
        articles = self._dedupe_and_sort(articles)[: self.settings.max_articles_per_request]
        if self.settings.extract_article_media:
            await self._enrich_missing_media(articles)
        return articles

    async def _fetch_rss(self, source: Source) -> list[Article]:
        ok, reason = is_safe_public_url(source.url, allow_localhost=not self.settings.source_url_private_networks_blocked)
        if not ok:
            logger.warning("RSS URL blocked by safety guard: %s (%s)", source.url, reason)
            return []
        cached = self._rss_cache.get(source.url)
        now = datetime.now(timezone.utc)
        if cached and (now - cached[0]).total_seconds() < self.settings.rss_cache_ttl_seconds:
            return list(cached[1])
        if feedparser is not None:
            parsed = await asyncio.to_thread(feedparser.parse, source.url)
            feed_title = source.title or parsed.feed.get("title") or urlparse(source.url).netloc
            result: list[Article] = []
            for entry in parsed.entries[:30]:
                title = strip_html(entry.get("title", "")).strip()
                url = _entry_article_link(entry)
                if not title or not url or not _is_valid_article_url(url):
                    continue
                summary = strip_html(entry.get("summary") or entry.get("description") or "")
                published = _parse_datetime(entry.get("published") or entry.get("created"))
                updated = _parse_datetime(entry.get("updated") or entry.get("modified"))
                media_url, media_type = _extract_feedparser_media(entry)
                result.append(
                    Article(
                        title=truncate(title, 220),
                        url=url,
                        summary=truncate(summary, 900),
                        source_name=truncate(feed_title, 120),
                        published_at=published or updated,
                        media_url=media_url,
                        media_type=media_type,
                        updated_at=updated,
                    )
                )
            self._rss_cache[source.url] = (now, list(result))
            return result

        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(source.url)
            resp.raise_for_status()
            xml_text = resp.text
        root = ET.fromstring(xml_text)
        channel_node = root.find("channel")
        feed_title = source.title or urlparse(source.url).netloc
        if channel_node is not None:
            title_node = channel_node.find("title")
            if title_node is not None and title_node.text:
                feed_title = title_node.text
            item_nodes = channel_node.findall("item")
        else:
            item_nodes = list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))

        result: list[Article] = []
        for item in item_nodes[:30]:
            title_node = _first_child(item, ["title", "{http://www.w3.org/2005/Atom}title"])
            link_node = _first_child(item, ["link", "{http://www.w3.org/2005/Atom}link"])
            summary_node = _first_child(item, ["description", "summary", "{http://www.w3.org/2005/Atom}summary"])
            pub_node = _first_child(item, ["pubDate", "published", "{http://www.w3.org/2005/Atom}published"])
            updated_node = _first_child(item, ["updated", "{http://www.w3.org/2005/Atom}updated"])
            title = strip_html(title_node.text if title_node is not None and title_node.text else "")
            link = ""
            if link_node is not None:
                link = link_node.get("href") or (link_node.text or "")
            if not title or not link or not _is_valid_article_url(link):
                continue
            media_url, media_type = _extract_xml_media(item)
            result.append(
                Article(
                    title=truncate(title, 220),
                    url=link.strip(),
                    summary=truncate(strip_html(summary_node.text if summary_node is not None and summary_node.text else ""), 900),
                    source_name=truncate(feed_title, 120),
                    published_at=_parse_datetime(pub_node.text if pub_node is not None else None),
                    media_url=media_url,
                    media_type=media_type,
                    updated_at=_parse_datetime(updated_node.text if updated_node is not None else None),
                )
            )
        self._rss_cache[source.url] = (now, list(result))
        return result

    async def _fetch_newsapi(self, query: str, language: str = "ru") -> list[Article]:
        cache_key = f"newsapi:{language}:{query}"
        now = datetime.now(timezone.utc)
        cached = self._search_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < self.settings.search_cache_ttl_seconds:
            return list(cached[1])
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "language": language,
            "sortBy": "publishedAt",
            "pageSize": min(10, self.settings.max_articles_per_request),
            "apiKey": self.settings.newsapi_key,
        }
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            logger.warning("NewsAPI request failed: %s", exc)
            return []

        result: list[Article] = []
        for item in payload.get("articles", []):
            title = strip_html(item.get("title") or "")
            article_url = item.get("url") or ""
            if not title or not _is_valid_article_url(article_url):
                continue
            image_url = item.get("urlToImage") or None
            if image_url:
                ok_media, _reason = is_safe_public_url(image_url, allow_localhost=not self.settings.source_url_private_networks_blocked)
                if not ok_media or _is_bad_media_asset(image_url):
                    image_url = None
            result.append(
                Article(
                    title=truncate(title, 220),
                    url=article_url,
                    summary=truncate(strip_html(item.get("description") or item.get("content") or ""), 900),
                    source_name=truncate((item.get("source") or {}).get("name") or "NewsAPI", 120),
                    published_at=_parse_datetime(item.get("publishedAt")),
                    media_url=image_url,
                    media_type="photo" if image_url else None,
                )
            )
        self._search_cache[cache_key] = (now, list(result))
        return result

    async def _fetch_brave(self, query: str) -> list[Article]:
        cache_key = f"brave:{query}"
        now = datetime.now(timezone.utc)
        cached = self._search_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < self.settings.search_cache_ttl_seconds:
            return list(cached[1])
        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.settings.brave_search_api_key,
        }
        params = {"q": query, "count": min(10, self.settings.max_articles_per_request), "freshness": "pd"}
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            logger.warning("Brave Search request failed: %s", exc)
            return []

        result: list[Article] = []
        for item in (payload.get("web") or {}).get("results", []):
            title = strip_html(item.get("title") or "")
            article_url = item.get("url") or ""
            if not title or not _is_valid_article_url(article_url):
                continue
            # Search thumbnails are frequently unrelated/logo images.
            # Keep Brave results as text sources; article-page OG media can be enriched later.
            media_url = None
            result.append(
                Article(
                    title=truncate(title, 220),
                    url=article_url,
                    summary=truncate(strip_html(item.get("description") or ""), 900),
                    source_name=truncate(urlparse(article_url).netloc or "Brave Search", 120),
                    published_at=None,
                    media_url=media_url,
                    media_type="photo" if media_url else None,
                )
            )
        self._search_cache[cache_key] = (now, list(result))
        return result

    async def _enrich_missing_media(self, articles: list[Article]) -> None:
        targets = [a for a in articles if not a.media_url][: self.settings.article_media_fetch_limit]
        if not targets:
            return
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "ChannelPilotAI/1.0"}) as client:
            tasks = [self._fetch_page_media(client, article) for article in targets]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_page_media(self, client: httpx.AsyncClient, article: Article) -> None:
        ok, reason = is_safe_public_url(article.url, allow_localhost=not self.settings.source_url_private_networks_blocked)
        if not ok:
            logger.debug("Article URL blocked by safety guard: %s (%s)", article.url, reason)
            return
        try:
            resp = await client.get(article.url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return
            media_url, media_type = _extract_meta_media(resp.text[:250_000], article.url)
            if media_url and media_type and not _is_bad_media_asset(media_url):
                ok_media, _reason = is_safe_public_url(media_url, allow_localhost=not self.settings.source_url_private_networks_blocked)
                if ok_media:
                    article.media_url = media_url
                    article.media_type = media_type
        except Exception as exc:
            logger.debug("Article media fetch failed for %s: %s", article.url, exc)

    @staticmethod
    def _dedupe_and_sort(articles: list[Article]) -> list[Article]:
        seen: set[str] = set()
        unique: list[Article] = []
        for article in articles:
            key = article.article_key
            if key in seen:
                continue
            seen.add(key)
            unique.append(article)
        unique.sort(key=lambda a: a.updated_at or a.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return unique
