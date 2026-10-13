"""Translations: English strings in `locales/en.yaml`, other languages beside it with the same keys.

`profile.language` picks the locale. A key missing from a translation falls back to English, so a
partial translation still works; that holds inside the label maps (`flows`, `symptoms`, `words_*`)
too, one entry at a time. The bot's own templates go out with `parse_mode: HTML`, so a
literal `<` or `&` in a string is written as `&lt;` or `&amp;`.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

import yaml

LOCALES = Path(__file__).parent / "locales"
DATE_FORMS = ("long", "short", "pick")


@lru_cache
def _load(locale: str) -> dict:
    path = LOCALES / f"{locale}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


class Translator:
    def __init__(self, locale: str = "en"):
        self.locale = locale
        en, own = _load("en"), _load(locale)
        self._strings = {**en, **own}
        for key, value in en.items():
            if isinstance(value, dict) and isinstance(own.get(key), dict):
                self._strings[key] = {**value, **own[key]}

    def __call__(self, key: str, **kw) -> str:
        s = self._strings.get(key, key)
        return s.format(**kw) if kw and isinstance(s, str) else s

    def date(self, d: dt.date, form: str = "long") -> str:
        """`long` «miércoles 21 de octubre», `short` «21 de octubre», `pick` «mié 21» (the day
        picker's buttons)."""
        if form not in DATE_FORMS:
            raise ValueError("unknown_date_form")
        weekday = self._strings["days_short" if form == "pick" else "days"][d.weekday()]
        month = self._strings["months"][d.month - 1]
        return self(f"date_{form}", weekday=weekday, day=d.day, month=month)

    def flow(self, flow: str) -> str:
        return self._strings["flows"][flow]

    def symptom(self, symptom: str) -> str:
        return self._strings["symptoms"][symptom]
