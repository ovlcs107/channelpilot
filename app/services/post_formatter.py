from __future__ import annotations

import re
from dataclasses import dataclass

# Conservative formatter for Telegram channel posts. It does not invent facts;
# it only normalizes spacing, headings and CTA placement before Telegram parse mode
# conversion happens.

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+|tg://[^\s)]+)\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+(?=[А-ЯA-ZЁ0-9])")

CTA_PATTERNS = (
    "Подписаться на",
    "TG Media Lab",
    "TMediaLabG",
)

@dataclass(slots=True)
class LayoutResult:
    text: str
    changed: bool


def _normalize_newlines(text: str) -> str:
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Convert common HTML line breaks to actual paragraph breaks before final sanitizer.
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _visible_text(line: str) -> str:
    line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
    line = re.sub(r"<\/?(?:b|strong|i|em|u|s|strike|del|blockquote|tg-spoiler)[^>]*>", "", line, flags=re.IGNORECASE)
    line = _MARKDOWN_LINK_RE.sub(r"\1", line)
    line = _HTML_TAG_RE.sub("", line)
    return line.strip()


def _is_probable_heading(line: str) -> bool:
    raw = (line or "").strip()
    if not raw:
        return False
    if raw.startswith((">", "—", "-", "•")):
        return False
    if any(token in raw for token in ("http://", "https://", "tg://")) and "[" not in raw:
        return False
    plain = _visible_text(raw)
    if not plain or len(plain) > 96:
        return False
    if plain.lower().startswith(("подписаться", "источник", "медиа", "канал:", "черновик")):
        return False
    # A heading usually has no sentence-ending period, or it is explicitly bold.
    return raw.startswith("**") or raw.lower().startswith("<b>") or not plain.endswith(".")


def _bold_heading(line: str) -> str:
    stripped = (line or "").strip()
    if not stripped:
        return stripped
    if stripped.startswith("**") or stripped.lower().startswith("<b>"):
        return stripped
    if stripped.startswith("#"):
        stripped = re.sub(r"^#{1,3}\s+", "", stripped).strip()
    return f"**{stripped}**"


def _is_cta_paragraph(paragraph: str) -> bool:
    lowered = paragraph.lower()
    return "подписаться" in lowered and ("tg media lab" in lowered or "tmedialabg" in lowered or "t.me" in lowered)


def _split_long_plain_paragraph(paragraph: str, limit: int = 230) -> list[str]:
    p = paragraph.strip()
    if len(p) <= limit:
        return [p]
    if p.startswith(">") or p.lower().startswith("<blockquote") or "```" in p:
        return [p]
    if _is_cta_paragraph(p):
        return [p]
    if _MARKDOWN_LINK_RE.search(p) and len(p) <= limit + 80:
        return [p]

    sentences = _SENTENCE_SPLIT_RE.split(p)
    if len(sentences) <= 1:
        return [p]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence.strip()
        if current and len(candidate) > limit:
            chunks.append(current.strip())
            current = sentence.strip()
        else:
            current = candidate
    if current:
        chunks.append(current.strip())
    return chunks or [p]


def normalize_channel_post_layout(text: str, *, force_heading: bool = True) -> str:
    """Make generated Telegram posts readable without changing their facts.

    The function intentionally works with markdown-ish input because later the
    publisher converts it to HTML/MarkdownV2 safely. It fixes the main visual
    problem: glued title/body/CTA and long unreadable paragraphs.
    """
    original = text or ""
    value = _normalize_newlines(original)
    if not value:
        return ""

    # Put CTA onto a standalone paragraph even if the model glued it to the body.
    value = re.sub(r"\s+(Подписаться на\s+\[[^\]]{1,120}\]\(https?://[^\s)]+\))\s*$", r"\n\n\1", value)
    value = re.sub(r"\s+(Подписаться на\s+TG Media Lab)\s*$", r"\n\n\1", value)

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", value) if p.strip()]
    if not paragraphs:
        return value.strip()

    result: list[str] = []

    # If the first paragraph contains a heading and body on separate lines, keep
    # the heading separated from the body.
    first_lines = [line.strip() for line in paragraphs[0].split("\n") if line.strip()]
    rest_first: list[str] = []
    if first_lines:
        first_line = first_lines[0]
        if force_heading and _is_probable_heading(first_line):
            result.append(_bold_heading(first_line))
            rest_first = first_lines[1:]
        else:
            rest_first = first_lines
    if rest_first:
        paragraphs[0] = " ".join(rest_first).strip()
    else:
        paragraphs = paragraphs[1:]

    for paragraph in paragraphs:
        if not paragraph:
            continue
        # Keep blockquotes as their own visual block.
        lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
        if lines and all(line.startswith(">") for line in lines):
            result.append("\n".join(lines))
            continue
        if paragraph.lower().startswith("<blockquote"):
            result.append(paragraph)
            continue
        for chunk in _split_long_plain_paragraph(" ".join(lines)):
            result.append(chunk)

    cleaned = "\n\n".join(part.strip() for part in result if part.strip())
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def normalize_channel_post_layout_result(text: str) -> LayoutResult:
    normalized = normalize_channel_post_layout(text)
    return LayoutResult(text=normalized, changed=normalized.strip() != (text or "").strip())
