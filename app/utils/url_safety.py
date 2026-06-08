from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

PRIVATE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def is_safe_public_url(url: str, *, allow_localhost: bool = False) -> tuple[bool, str]:
    """Cheap SSRF guard for user-provided URLs.

    It rejects non-http(s) schemes, empty hosts, localhost and literal private IPs.
    This is intentionally conservative; DNS rebinding protection should be handled
    by infrastructure too, but this blocks the most common mistakes immediately.
    """
    raw = (url or "").strip()
    if not raw:
        return False, "пустая ссылка"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return False, "разрешены только http/https ссылки"
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False, "в ссылке нет домена"
    if not allow_localhost and host in PRIVATE_HOSTS:
        return False, "localhost/private host запрещён"
    try:
        ip = ipaddress.ip_address(host)
        if not allow_localhost and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved):
            return False, "private/internal IP запрещён"
    except ValueError:
        # Normal domain name. We deliberately do not resolve DNS here to avoid
        # blocking requests and unexpected resolver behavior inside Railway.
        pass
    return True, "ok"
