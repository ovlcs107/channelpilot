from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any


def estimate_tokens_from_text(text: str) -> int:
    # Rough but stable budget estimate for mixed Russian/English prompts.
    # Real tokenizer is provider-specific; this is intentionally conservative.
    return max(1, int(len(text) / 3.4) + 1)


def canonical_prompt(prompt: list[dict[str, str]]) -> str:
    return json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prompt_hash(prompt_text: str, *, model: str, purpose: str) -> str:
    return hashlib.sha256(f"{model}|{purpose}|{prompt_text}".encode("utf-8")).hexdigest()


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_start_utc() -> datetime:
    now = utcnow_naive()
    return datetime(now.year, now.month, now.day)


def month_start_utc() -> datetime:
    now = utcnow_naive()
    return datetime(now.year, now.month, 1)


def cache_cutoff(hours: int) -> datetime:
    return utcnow_naive() - timedelta(hours=hours)
