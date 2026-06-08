from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from app.services.autopilot_window import due_slots_for_channel


def channel(times: str):
    return SimpleNamespace(daily_times=times)


def test_due_slots_accepts_late_scheduler_inside_window():
    now = datetime(2026, 6, 5, 13, 18, 34, tzinfo=ZoneInfo("Europe/Berlin"))
    slots = due_slots_for_channel(channel("13:18"), now, 5)
    assert len(slots) == 1
    assert slots[0].slot_key == "2026-06-05 13:18"


def test_due_slots_rejects_outside_window():
    now = datetime(2026, 6, 5, 13, 24, 1, tzinfo=ZoneInfo("Europe/Berlin"))
    slots = due_slots_for_channel(channel("13:18"), now, 5)
    assert slots == []


def test_due_slots_ignores_invalid_times():
    now = datetime(2026, 6, 5, 13, 18, 1, tzinfo=ZoneInfo("Europe/Berlin"))
    slots = due_slots_for_channel(channel("bad,25:99,13:18"), now, 5)
    assert len(slots) == 1
