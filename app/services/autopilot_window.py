from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class DueSlot:
    slot_key: str
    scheduled_hm: str
    lateness_seconds: int


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hour_raw, minute_raw = value.split(":", 1)
        hour = int(hour_raw)
        minute = int(minute_raw)
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def due_slots_for_channel(channel, now: datetime, window_minutes: int) -> list[DueSlot]:
    """Return schedule slots that are due now and still inside the tolerance window."""
    result: list[DueSlot] = []
    window = timedelta(minutes=max(0, window_minutes))
    for raw in (getattr(channel, "daily_times", "") or "").split(","):
        raw = raw.strip()
        parsed = _parse_hhmm(raw)
        if parsed is None:
            continue
        hour, minute = parsed
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled <= now <= scheduled + window:
            result.append(
                DueSlot(
                    slot_key=scheduled.strftime("%Y-%m-%d %H:%M"),
                    scheduled_hm=scheduled.strftime("%H:%M"),
                    lateness_seconds=int((now - scheduled).total_seconds()),
                )
            )
    return result
