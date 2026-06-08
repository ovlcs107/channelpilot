from __future__ import annotations

import json
from datetime import datetime, timezone


def export_channel_config(channel, sources=None, rules=None) -> str:
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "channel": {
            "username": channel.username,
            "title": channel.title,
            "topic": channel.topic,
            "style": channel.style,
            "style_profile_json": channel.style_profile_json,
            "editorial_prompt": channel.editorial_prompt,
            "mode": channel.mode,
            "media_enabled": channel.media_enabled,
            "daily_times": channel.daily_times,
            "min_sources": channel.min_sources,
        },
        "sources": [{"url": s.url, "title": s.title, "enabled": s.enabled, "priority": s.priority} for s in (sources or [])],
        "rules": [{"domain": r.domain, "rule_type": r.rule_type, "note": r.note} for r in (rules or [])],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
