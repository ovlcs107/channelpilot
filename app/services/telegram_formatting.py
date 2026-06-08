from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlparse

from app.utils.text import strip_html
from app.services.post_formatter import normalize_channel_post_layout

FORMAT_PLAIN = "plain"
FORMAT_HTML = "html"
FORMAT_MARKDOWN_V2 = "markdown_v2"
SUPPORTED_FORMAT_MODES = {FORMAT_PLAIN, FORMAT_HTML, FORMAT_MARKDOWN_V2}

_ALLOWED_SIMPLE_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "blockquote", "tg-spoiler"}
_MARKDOWN_V2_SPECIALS = r"_ * [ ] ( ) ~ ` > # + - = | { } . !".split()
_TAG_RE = re.compile(r"<[^>]+>")


def _convert_markdown_blockquotes_to_html(text: str) -> str:
    lines: list[str] = []
    quote_buffer: list[str] = []

    def flush_quote() -> None:
        nonlocal quote_buffer
        if quote_buffer:
            body = "\n".join(html.escape(line.strip(), quote=False) for line in quote_buffer if line.strip())
            if body:
                lines.append(f"<blockquote>{body}</blockquote>")
            quote_buffer = []

    for line in (text or "").splitlines():
        if line.lstrip().startswith(">"):
            quote_buffer.append(line.lstrip()[1:].strip())
        else:
            flush_quote()
            lines.append(line)
    flush_quote()
    return "\n".join(lines)


def normalize_format_mode(value: str | None, default: str = FORMAT_HTML) -> str:
    raw = (value or default or FORMAT_HTML).strip().lower().replace("-", "_")
    aliases = {
        "md": FORMAT_MARKDOWN_V2,
        "markdown": FORMAT_MARKDOWN_V2,
        "markdownv2": FORMAT_MARKDOWN_V2,
        "markdown_2": FORMAT_MARKDOWN_V2,
        "mkv2": FORMAT_MARKDOWN_V2,
        "rich": FORMAT_HTML,
        "telegram_html": FORMAT_HTML,
        "off": FORMAT_PLAIN,
        "none": FORMAT_PLAIN,
        "text": FORMAT_PLAIN,
    }
    raw = aliases.get(raw, raw)
    return raw if raw in SUPPORTED_FORMAT_MODES else FORMAT_HTML


def parse_mode_for_format(mode: str | None) -> str | None:
    normalized = normalize_format_mode(mode)
    if normalized == FORMAT_HTML:
        return "HTML"
    if normalized == FORMAT_MARKDOWN_V2:
        return "MarkdownV2"
    return None


def strip_telegram_formatting(text: str) -> str:
    """Return readable text without Telegram parse-mode formatting."""
    return strip_html(text or "")


def format_for_telegram(text: str, mode: str | None = FORMAT_HTML) -> str:
    """Prepare user/AI text for Telegram parse_mode.

    HTML is the default because it is much less fragile than MarkdownV2.
    MarkdownV2 is supported as a safe escaped mode; it preserves readability and
    prevents Telegram entity crashes, but it intentionally does not try to infer
    advanced formatting from generated text.
    """
    normalized = normalize_format_mode(mode)
    text = normalize_channel_post_layout(text or "")
    if normalized == FORMAT_PLAIN:
        return strip_telegram_formatting(text)
    if normalized == FORMAT_MARKDOWN_V2:
        return markdown_to_telegram_v2(strip_telegram_formatting(text))
    return sanitize_telegram_html(markdownish_to_html(text))


def markdownish_to_html(text: str) -> str:
    """Convert a small, safe subset of common markdown into Telegram HTML.

    This lets the model write **bold** headings or simple # headings, while the
    publisher still sends robust HTML to Telegram.
    """
    text = text or ""
    text = _convert_markdown_blockquotes_to_html(text)

    def heading_repl(match: re.Match[str]) -> str:
        return f"<b>{html.escape(match.group(1).strip(), quote=False)}</b>"

    # Convert headings line-by-line first.
    text = re.sub(r"(?m)^\s{0,3}#{1,3}\s+(.+?)\s*$", heading_repl, text)

    # Inline code: escape code body to avoid accidental tags inside code.
    text = re.sub(r"`([^`\n]{1,180})`", lambda m: f"<code>{html.escape(m.group(1), quote=False)}</code>", text)

    # Markdown links -> Telegram HTML links. This is used for clean CTA footers
    # like: Подписаться на [TG Media Lab](https://t.me/TMediaLabG).
    def link_repl(match: re.Match[str]) -> str:
        label = html.escape(match.group(1).strip(), quote=False)
        url = html.escape(match.group(2).strip(), quote=True)
        return f'<a href="{url}">{label}</a>'

    text = re.sub(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+|tg://[^\s)]+)\)", link_repl, text)

    # Conservative rich marks. Avoid matching across newlines.
    text = re.sub(r"\*\*([^*\n]{1,220})\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_\n]{1,220})__", r"<u>\1</u>", text)
    text = re.sub(r"~~([^~\n]{1,220})~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<!~)~([^~\n]{1,220})~(?!~)", r"<s>\1</s>", text)
    text = re.sub(r"\|\|([^|\n]{1,260})\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    return text


class _TelegramHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.stack: list[str] = []

    @staticmethod
    def _safe_href(value: str) -> str:
        value = (value or "").strip()
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https", "tg"}:
            return value
        if parsed.netloc.endswith("t.me") or parsed.netloc.endswith("telegram.me"):
            return value
        return ""

    @staticmethod
    def _canonical_tag(tag: str, attrs: Iterable[tuple[str, str | None]]) -> tuple[str, str] | None:
        tag = (tag or "").lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "span" and attrs_dict.get("class") == "tg-spoiler":
            return "tg-spoiler", "<tg-spoiler>"
        if tag == "a":
            href = _TelegramHTMLSanitizer._safe_href(attrs_dict.get("href", ""))
            if href:
                return "a", f'<a href="{html.escape(href, quote=True)}">'
            return None
        if tag in _ALLOWED_SIMPLE_TAGS:
            # Telegram accepts aliases like strong/em/ins/strike/del. Keep them,
            # but strip all attributes except href handled above.
            return tag, f"<{tag}>"
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        item = self._canonical_tag(tag, attrs)
        if not item:
            return
        canonical, rendered = item
        self.parts.append(rendered)
        self.stack.append(canonical)

    def handle_endtag(self, tag: str) -> None:
        tag = (tag or "").lower()
        if tag == "span":
            tag = "tg-spoiler"
        if tag not in self.stack:
            return
        # Close nested tags safely up to the requested tag.
        while self.stack:
            current = self.stack.pop()
            self.parts.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data or "", quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>" )
        return "".join(self.parts).strip()


def sanitize_telegram_html(text: str) -> str:
    parser = _TelegramHTMLSanitizer()
    try:
        parser.feed(text or "")
        parser.close()
        return parser.get_html()
    except Exception:
        # Last-resort safety: never let broken AI markup crash the bot.
        return html.escape(strip_html(text or ""), quote=False)


def escape_markdown_v2(text: str) -> str:
    text = text or ""
    # Telegram MarkdownV2 requires escaping these characters almost everywhere.
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}\.!])", r"\\\1", text)


def channel_format_mode(channel, default: str = FORMAT_HTML) -> str:
    import json

    try:
        profile = json.loads(getattr(channel, "style_profile_json", "{}") or "{}")
    except Exception:
        profile = {}
    return normalize_format_mode(str(profile.get("format_mode") or default or FORMAT_HTML))

# --- v12: safe Telegram MarkdownV2 renderer for bot DM UI and AI-authored markdown ---
def _escape_markdown_v2_segment(text: str) -> str:
    return escape_markdown_v2(text or "")


def markdown_to_telegram_v2(text: str) -> str:
    """Render a safe subset of human/AI Markdown to Telegram MarkdownV2.

    Supported input:
    - **bold** -> *bold*
    - *italic* -> _italic_
    - `code` -> `code`
    - [label](https://example.com) links
    - # headings -> bold lines
    - > quote lines -> Telegram blockquote
    - ~~strike~~ or ~strike~ -> strikethrough
    - ||spoiler|| -> spoiler

    Everything else is escaped, so broken AI markdown should not crash Telegram.
    """
    text = (text or "").replace("\r\n", "\n")
    text = re.sub(r"(?m)^\s{0,3}#{1,3}\s+(.+?)\s*$", r"**\1**", text)

    placeholders: dict[str, str] = {}

    def put(rendered: str) -> str:
        key = f"\uE000{len(placeholders)}\uE001"
        placeholders[key] = rendered
        return key

    def code_repl(match: re.Match[str]) -> str:
        body = match.group(1).replace("\\", "\\\\").replace("`", "\\`")
        return put(f"`{body}`")

    def link_repl(match: re.Match[str]) -> str:
        label = _escape_markdown_v2_segment(match.group(1))
        url = match.group(2).strip().replace("\\", "\\\\").replace(")", "\\)")
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
            return match.group(0)
        return put(f"[{label}]({url})")

    def bold_repl(match: re.Match[str]) -> str:
        body = _escape_markdown_v2_segment(match.group(1).strip())
        return put(f"*{body}*")

    def italic_repl(match: re.Match[str]) -> str:
        body = _escape_markdown_v2_segment(match.group(1).strip())
        return put(f"_{body}_")

    def strike_repl(match: re.Match[str]) -> str:
        body = _escape_markdown_v2_segment(match.group(1).strip())
        return put(f"~{body}~")

    def spoiler_repl(match: re.Match[str]) -> str:
        body = _escape_markdown_v2_segment(match.group(1).strip())
        return put(f"||{body}||")

    def quote_repl(match: re.Match[str]) -> str:
        body = _escape_markdown_v2_segment(match.group(1).strip())
        return put(f">{body}")

    # Protect code, links and block-level quote syntax first.
    text = re.sub(r"`([^`\n]{1,240})`", code_repl, text)
    text = re.sub(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+|tg://[^\s)]+)\)", link_repl, text)
    text = re.sub(r"(?m)^\s*>\s?(.{1,700})$", quote_repl, text)
    text = re.sub(r"\|\|([^|\n]{1,260})\|\|", spoiler_repl, text)
    text = re.sub(r"~~([^~\n]{1,260})~~", strike_repl, text)
    text = re.sub(r"(?<!~)~([^~\n]{1,220})~(?!~)", strike_repl, text)
    text = re.sub(r"\*\*([^*\n]{1,300})\*\*", bold_repl, text)
    # Conservative italic: avoids breaking bullet lists or multiplication-looking text.
    text = re.sub(r"(?<!\*)\*([^*\n]{1,180})\*(?!\*)", italic_repl, text)

    escaped = _escape_markdown_v2_segment(text)
    for key, rendered in placeholders.items():
        escaped = escaped.replace(_escape_markdown_v2_segment(key), rendered)
    return escaped


def format_dm_markdown(text: str) -> str:
    """Format internal bot DM messages as Telegram MarkdownV2."""
    return markdown_to_telegram_v2(text)
