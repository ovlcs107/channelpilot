from __future__ import annotations

import json
from typing import Any

SECRET_KEYS = {"token", "api_key", "database_url", "password", "secret", "telegram_bot_token", "ai_api_key"}


def sanitize_payload(value: Any, max_chars: int = 1800) -> str:
    def clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                key = str(k).lower()
                if any(secret in key for secret in SECRET_KEYS):
                    out[k] = "***"
                else:
                    out[k] = clean(v)
            return out
        if isinstance(obj, list):
            return [clean(x) for x in obj[:20]]
        if isinstance(obj, str):
            return obj[:500]
        return obj
    try:
        text = json.dumps(clean(value), ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


async def add_audit_event(session, *, user_id: int = 0, role: str = "", channel=None, action: str, payload: Any = None, result: str = "success", reason: str = "", draft_id: int | None = None) -> None:
    from app.models import AuditEvent

    event = AuditEvent(
        user_id=user_id or 0,
        role=role or "unknown",
        channel_id=getattr(channel, "id", None) if channel is not None else None,
        channel_username=getattr(channel, "username", None) if channel is not None else None,
        action_type=action,
        action_payload_json=sanitize_payload(payload or {}),
        result=result,
        reason=reason[:500] if reason else "",
        related_draft_id=draft_id,
    )
    session.add(event)
