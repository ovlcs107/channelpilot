from __future__ import annotations

import re
from app.services.anti_cringe import detect_ai_smells
from dataclasses import dataclass, field


@dataclass(slots=True)
class EditorialReview:
    score: int
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.score >= 70 and not any("критично" in w.lower() for w in self.warnings)

    def summary(self) -> str:
        if not self.warnings:
            return f"Проверка текста: {self.score}/100, критичных замечаний нет."
        return f"Проверка текста: {self.score}/100. Замечания: " + "; ".join(self.warnings[:5])


_CLICHES = [
    "революционн", "уникальн", "взорв", "шок", "срочно", "никто не ожидал",
    "гарантирован", "100%", "все в шоке",
]


def review_post_text(text: str, *, require_source: bool = False, digest: bool = False) -> EditorialReview:
    text = text or ""
    clean = re.sub(r"<[^>]+>", "", text)
    score = 100
    warnings: list[str] = []

    if len(clean.strip()) < 120:
        score -= 20
        warnings.append("текст выглядит слишком коротким")
    if len(clean) > (2800 if digest else 1800):
        score -= 18
        warnings.append("текст длинноват для Telegram")

    first_line = next((line.strip() for line in clean.splitlines() if line.strip()), "")
    if not first_line or len(first_line) > 110:
        score -= 12
        warnings.append("первый заголовок отсутствует или слишком длинный")

    lowered = clean.lower()
    for item in _CLICHES:
        if item in lowered:
            score -= 8
            warnings.append("возможный кликбейт/инфоцыганский оборот")
            break

    if require_source and "источник" not in lowered and "источники" not in lowered:
        score -= 22
        warnings.append("критично: нет блока источников")

    if digest:
        blocks = len(re.findall(r"(?m)^\s*(?:\d+[\.)]|[-•]|<b>|\*\*)", text))
        if blocks < 2:
            score -= 12
            warnings.append("для дайджеста мало отдельных блоков")

    # Markdown/HTML smell checks — not fatal because publisher has fallback.
    if text.count("**") % 2 != 0 or text.count("||") % 2 != 0:
        score -= 8
        warnings.append("возможна незакрытая Markdown-разметка")
    if text.count("<b>") != text.count("</b>"):
        score -= 8
        warnings.append("возможна незакрытая HTML-разметка")

    smells = detect_ai_smells(clean)
    if smells:
        score -= min(18, 6 * len(smells))
        warnings.append("есть нейросеточные клише: " + ", ".join(smells[:3]))

    return EditorialReview(score=max(0, min(100, score)), warnings=warnings)
