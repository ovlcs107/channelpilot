from __future__ import annotations

import hashlib
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.utils.url_safety import is_safe_public_url

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadedMedia:
    path: Path
    media_type: str
    content_type: str


class MediaManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage_dir = Path(settings.media_storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    async def prepare_media(self, media_url: str, media_type: str | None) -> DownloadedMedia | None:
        if not self.settings.media_download_enabled:
            return None
        ok, reason = is_safe_public_url(media_url, allow_localhost=not self.settings.source_url_private_networks_blocked)
        if not ok:
            logger.warning("Media URL blocked by safety guard: %s (%s)", media_url, reason)
            return None

        url_hash = hashlib.sha256(media_url.encode("utf-8")).hexdigest()
        guessed_ext = self._guess_extension(media_url, "")
        existing = self.storage_dir / f"{url_hash}{guessed_ext}"
        if existing.exists() and existing.stat().st_size > 0:
            return DownloadedMedia(path=existing, media_type=media_type or self._type_from_extension(existing.suffix), content_type=mimetypes.guess_type(existing.name)[0] or "application/octet-stream")

        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "ChannelPilotAI/1.0"}) as client:
                response = await client.get(media_url)
                response.raise_for_status()
                content = response.content
                if len(content) > self.settings.media_max_bytes:
                    logger.warning("Media too large: %s bytes from %s", len(content), media_url)
                    return None
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        except Exception as exc:
            logger.warning("Media download failed for %s: %s", media_url, exc)
            return None

        detected_type = media_type or self._type_from_content_type(content_type) or self._type_from_extension(urlparse(media_url).path)
        if detected_type not in {"photo", "video"}:
            return None
        extension = self._guess_extension(media_url, content_type)
        path = self.storage_dir / f"{url_hash}{extension}"
        path.write_bytes(content)
        return DownloadedMedia(path=path, media_type=detected_type, content_type=content_type or "application/octet-stream")

    @staticmethod
    def _type_from_content_type(content_type: str) -> str | None:
        if content_type.startswith("image/"):
            return "photo"
        if content_type.startswith("video/"):
            return "video"
        return None

    @staticmethod
    def _type_from_extension(path_or_suffix: str) -> str:
        lower = path_or_suffix.lower()
        if lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return "photo"
        if lower.endswith((".mp4", ".mov", ".m4v", ".webm")):
            return "video"
        return "photo"

    @staticmethod
    def _guess_extension(media_url: str, content_type: str) -> str:
        path = urlparse(media_url).path.lower()
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm"):
            if path.endswith(ext):
                return ext
        guessed = mimetypes.guess_extension(content_type or "")
        if guessed in {".jpe"}:
            return ".jpg"
        if guessed:
            return guessed
        return ".bin"
