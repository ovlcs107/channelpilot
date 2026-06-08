from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases that make posts look obviously machine-written or low-quality.
# Keep this list conservative: we rewrite only the worst boilerplate, not the author's voice.
ANTI_CRINGE_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bв мире ([^\n\.]{3,80}) нарастает ажиотаж\b", re.IGNORECASE), r"вокруг \1 снова много обсуждений"),
    (re.compile(r"\bэто показывает, насколько ([^\n\.]{3,120})\b", re.IGNORECASE), "это хорошо видно по реакции аудитории"),
    (re.compile(r"\bготовы идти на крайние меры ради\b", re.IGNORECASE), "готовы на странные поступки ради"),
    (re.compile(r"\bсможет ли ([^\n\?]{3,90}) занять свою нишу на рынке\?", re.IGNORECASE), r"Получится ли у \1 закрепиться на рынке?"),
    (re.compile(r"\bвремя покажет\b", re.IGNORECASE), "посмотрим, как тема будет развиваться"),
    (re.compile(r"\bостается только ждать\b", re.IGNORECASE), "следим за развитием истории"),
    (re.compile(r"\bне оставляет равнодушными\b", re.IGNORECASE), "активно обсуждается"),
]

FORBIDDEN_AI_SMELLS = [
    "в мире видеоигр нарастает ажиотаж",
    "в мире технологий нарастает ажиотаж",
    "это показывает, насколько фанаты готовы",
    "готовы идти на крайние меры",
    "сможет ли занять свою нишу",
    "время покажет",
    "остается только ждать",
    "никого не оставляет равнодушным",
]

@dataclass(slots=True)
class AntiCringeResult:
    text: str
    changed: bool
    hits: list[str]


def detect_ai_smells(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [item for item in FORBIDDEN_AI_SMELLS if item in lowered]


def sanitize_post_text(text: str) -> AntiCringeResult:
    result = text or ""
    hits: list[str] = []
    for pattern, replacement in ANTI_CRINGE_REPLACEMENTS:
        if pattern.search(result):
            hits.append(pattern.pattern)
            result = pattern.sub(replacement, result)
    # Remove double spaces introduced by replacements while keeping Telegram line breaks.
    result = re.sub(r"[ \t]{2,}", " ", result)
    return AntiCringeResult(text=result.strip(), changed=result.strip() != (text or "").strip(), hits=hits)
