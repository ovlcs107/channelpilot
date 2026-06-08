from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class SourcePackItem:
    title: str
    url: str
    priority: int = 0
    rules: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


@dataclass(frozen=True, slots=True)
class SourcePack:
    key: str
    title: str
    description: str
    language: str
    region: str
    sources: tuple[SourcePackItem, ...]


class SourcePackRegistry:
    def __init__(self, path: str | Path = "data/source_packs.json") -> None:
        self.path = Path(path)
        self._packs: dict[str, SourcePack] | None = None

    def list_packs(self) -> list[SourcePack]:
        return list(self._load().values())

    def get(self, key: str) -> SourcePack | None:
        return self._load().get(key.strip().lower())

    def require(self, key: str) -> SourcePack:
        pack = self.get(key)
        if pack is None:
            known = ", ".join(sorted(self._load().keys()))
            raise KeyError(f"Пакет источников не найден: {key}. Доступны: {known}")
        return pack

    def _load(self) -> dict[str, SourcePack]:
        if self._packs is not None:
            return self._packs
        if not self.path.exists():
            self._packs = {}
            return self._packs
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        packs: dict[str, SourcePack] = {}
        for key, payload in raw.items():
            items = tuple(
                SourcePackItem(
                    title=str(item.get("title") or item.get("url") or "Источник"),
                    url=str(item["url"]),
                    priority=int(item.get("priority") or 0),
                    rules=tuple(str(rule) for rule in item.get("rules", [])),
                )
                for item in payload.get("sources", [])
                if item.get("url")
            )
            packs[key] = SourcePack(
                key=key,
                title=str(payload.get("title") or key),
                description=str(payload.get("description") or ""),
                language=str(payload.get("language") or "mixed"),
                region=str(payload.get("region") or "WORLD"),
                sources=items,
            )
        self._packs = packs
        return packs
