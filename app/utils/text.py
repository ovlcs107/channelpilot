from __future__ import annotations

import html
import re
from typing import Iterable

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = _TAG_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def truncate(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def split_long_message(text: str, limit: int = 3900) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = paragraph
            while len(current) > limit:
                chunks.append(current[:limit])
                current = current[limit:]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def bullet_list(items: Iterable[str], prefix: str = "— ") -> str:
    return "\n".join(f"{prefix}{item}" for item in items)
