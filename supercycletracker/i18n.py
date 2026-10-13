"""Translations: English strings in `locales/en.yaml`, other languages beside it with the same keys.

`profile.language` picks the locale. A key missing from a translation falls back to English, so a
partial translation still works. The bot's own templates go out with `parse_mode: HTML`, so a
literal `<` or `&` in a string is written as `&lt;` or `&amp;`.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

import yaml

LOCALES = Path(__file__).parent / "locales"
DATE_FORMS = ("long", "short", "day", "pick")


@lru_cache
def _load(locale: str) -> dict:
    path = LOCALES / f"{locale}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def available() -> list[str]:
    return sorted(p.stem for p in LOCALES.glob("*.yaml"))


class Translator:
    def __init__(self, locale: str = "en"):
        self.locale = locale
        self._strings = {**_load("en"), **_load(locale)}

    def __call__(self, key: str, **kw) -> str:
        s = self._strings.get(key, key)
        return s.format(**kw) if kw and isinstance(s, str) else s

    def raw(self, key: str):
        return self._strings.get(key)

    def date(self, d: dt.date, form: str = "long") -> str:
        """`long` «miércoles 21 de octubre», `short` «21 de octubre», `day` «miércoles 21»,
        `pick` «mié 21» (the day picker's buttons)."""
        if form not in DATE_FORMS:
            raise ValueError("unknown_date_form")
        weekday = self._strings["days_short" if form == "pick" else "days"][d.weekday()]
        month = self._strings["months"][d.month - 1]
        return self(f"date_{form}", weekday=weekday, day=d.day, month=month)

    def flow(self, flow: str) -> str:
        return self._strings["flows"][flow]

    def symptom(self, symptom: str) -> str:
        return self._strings["symptoms"][symptom]
