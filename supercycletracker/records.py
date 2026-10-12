"""The records file `cycle/log.jsonl`: one JSON object per date, sorted, in a fixed key order.

Everything here is pure except `load` and `save`. A write is `apply(log, edit, source)`, which
re-derives every `cycle_start` with the 10-day rule and returns a `Change`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

FLOWS = ("none", "unspecified", "light", "medium", "heavy")
BLEED = frozenset({"unspecified", "light", "medium", "heavy"})
SYMPTOMS = (
    "abdominal_cramps",
    "bloating",
    "headache",
    "lower_back_pain",
    "pelvic_pain",
    "breast_pain",
    "nausea",
    "diarrhea",
    "constipation",
    "fatigue",
    "mood_changes",
    "acne",
    "sleep_changes",
    "appetite_changes",
    "clots",
    "spotting",
)
KEYS = ("date", "flow", "cycle_start", "pain", "symptoms", "note", "source")
SOURCES = ("telegram", "admin")
IMPORT_SOURCE = re.compile(r"^apple-health-export-\d{4}-\d{2}-\d{2}$")

START_GAP_DAYS = 10  # a bleed day with no bleed day in the 10 days before it is a start
MAX_BACK_DAYS = 90  # a block or a button may write dates in [today - 90, today]
MAX_BLOCK_DAYS = 14
MAX_NOTE = 500
DAY_FIELDS = ("flow", "pain", "symptoms", "note")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Log = dict[date, "Day"]


@dataclass(frozen=True)
class Day:
    date: date
    source: str
    flow: str | None = None
    cycle_start: bool = False
    pain: int | None = None
    symptoms: tuple[str, ...] = ()
    note: str | None = None

    @property
    def bleed(self) -> bool:
        return self.flow in BLEED

    @property
    def empty(self) -> bool:
        """No fact left: no flow, pain, symptom or note."""
        return self.flow is None and self.pain is None and not self.symptoms and not self.note

    def to_dict(self) -> dict:
        out: dict = {"date": self.date.isoformat()}
        if self.flow is not None:
            out["flow"] = self.flow
        if self.cycle_start:
            out["cycle_start"] = True
        if self.pain is not None:
            out["pain"] = self.pain
        if self.symptoms:
            out["symptoms"] = list(self.symptoms)
        if self.note:
            out["note"] = self.note
        out["source"] = self.source
        return out

    def to_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, obj: Mapping) -> Day:
        unknown = set(obj) - set(KEYS)
        if unknown:
            raise ValueError("unknown_key")
        symptoms = obj.get("symptoms") or ()
        return cls(
            date=_parse_date(obj["date"]),
            source=str(obj["source"]),
            flow=obj.get("flow"),
            cycle_start=obj.get("cycle_start") is True,
            pain=obj.get("pain"),
            symptoms=tuple(sorted(set(symptoms))),
            note=obj.get("note") or None,
        )


# --- file ----------------------------------------------------------------------------------


def load(path: str | Path) -> Log:
    """Read the log. A missing file is an empty log."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    log: Log = {}
    for line in text.splitlines():
        if line.strip():
            day = Day.from_dict(json.loads(line))
            log[day.date] = day
    return log


def dumps(log: Mapping[date, Day]) -> str:
    return "".join(log[d].to_line() + "\n" for d in sorted(log))


def save(path: str | Path, log: Mapping[date, Day]) -> None:
    """Write the log atomically: a temp file in the same directory, then `os.replace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".log.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dumps(log))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- cycle starts --------------------------------------------------------------------------


def starts(log: Mapping[date, Day]) -> list[date]:
    """Dates that start a cycle: a bleed day with no bleed day in `[d - 10, d - 1]`."""
    bleeds = sorted(d for d, day in log.items() if day.bleed)
    out: list[date] = []
    previous: date | None = None
    for d in bleeds:
        if previous is None or (d - previous).days > START_GAP_DAYS:
            out.append(d)
        previous = d
    return out


def derive_starts(log: Mapping[date, Day]) -> Log:
    """The same log with every `cycle_start` recomputed by the 10-day rule."""
    first = set(starts(log))
    return {
        d: (day if day.cycle_start == (d in first) else replace(day, cycle_start=d in first))
        for d, day in sorted(log.items())
    }


# --- edits ---------------------------------------------------------------------------------


class BlockError(ValueError):
    """A refused record block. The message is a reason code, never the offending value."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Edit:
    """A validated write: per-date field replacements and whole-date deletions.

    `days` maps a date to the fields given for it; a field left out stays as it is. `symptoms`
    is the day's whole list (empty clears it), `note` "" clears the note.
    """

    days: Mapping[date, Mapping[str, object]]
    delete: frozenset[date] = frozenset()


def in_range(d: date, today: date) -> bool:
    return today - timedelta(days=MAX_BACK_DAYS) <= d <= today


def validate_block(block: object, today: date) -> Edit:
    """Check a record block, all or nothing. Raises `BlockError` with a reason code."""
    if not isinstance(block, Mapping):
        raise BlockError("bad_block")
    if set(block) - {"days", "delete"}:
        raise BlockError("unknown_key")
    items = block.get("days", [])
    deletes = block.get("delete", [])
    if not isinstance(items, list) or not isinstance(deletes, list):
        raise BlockError("bad_block")
    if len(items) + len(deletes) > MAX_BLOCK_DAYS:
        raise BlockError("too_many_days")

    days: dict[date, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise BlockError("bad_block")
        if set(item) - {"date", *DAY_FIELDS}:
            raise BlockError("unknown_key")
        d = _block_date(item.get("date"), today)
        if d in days:
            raise BlockError("duplicate_date")
        fields = {k: item[k] for k in DAY_FIELDS if k in item}
        if not fields:
            raise BlockError("empty_day")
        days[d] = _check_fields(fields)

    delete: set[date] = set()
    for raw in deletes:
        d = _block_date(raw, today)
        if d in days or d in delete:
            raise BlockError("duplicate_date")
        delete.add(d)
    return Edit(days=days, delete=frozenset(delete))


def _block_date(raw: object, today: date) -> date:
    try:
        d = _parse_date(raw)
    except (TypeError, ValueError):
        raise BlockError("bad_date") from None
    if not in_range(d, today):
        raise BlockError("date_out_of_range")
    return d


def _check_fields(fields: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    if "flow" in fields:
        if fields["flow"] not in FLOWS:
            raise BlockError("unknown_flow")
        out["flow"] = fields["flow"]
    if "pain" in fields:
        pain = fields["pain"]
        if type(pain) is not int or not 0 <= pain <= 10:
            raise BlockError("bad_pain")
        out["pain"] = pain
    if "symptoms" in fields:
        symptoms = fields["symptoms"]
        if not isinstance(symptoms, list) or not all(isinstance(s, str) for s in symptoms):
            raise BlockError("bad_symptoms")
        if any(s not in SYMPTOMS for s in symptoms):
            raise BlockError("unknown_symptom")
        out["symptoms"] = tuple(sorted(set(symptoms)))
    if "note" in fields:
        note = fields["note"]
        if not isinstance(note, str):
            raise BlockError("bad_note")
        if len(note) > MAX_NOTE:
            raise BlockError("note_too_long")
        out["note"] = note.strip()
    return out


# --- applying ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """What a write did. `before` and `after` are the whole logs (the phase can be computed on
    both); `items` lists each date the edit named whose line actually changed, oldest first, as
    `(date, line before or None, line after or None)`. An edit that changed nothing is empty."""

    before: Mapping[date, Day]
    after: Mapping[date, Day]
    items: tuple[tuple[date, Day | None, Day | None], ...]

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(d for d, _, _ in self.items)


def apply(log: Mapping[date, Day], edit: Edit, source: str) -> Change:
    """Apply an edit with replace-field semantics, re-derive starts, and return the `Change`.

    `source` becomes the source of every date written. A day left with no fact (no flow, pain,
    symptom or note) is removed, so a line always says something.
    """
    _check_source(source)
    new = dict(log)
    for d in edit.delete:
        new.pop(d, None)
    for d, fields in edit.days.items():
        old = new.get(d) or Day(date=d, source=source)
        day = replace(old, source=source, **_normalised(fields))
        if day.empty:
            new.pop(d, None)
        else:
            new[d] = day
    after = derive_starts(new)
    touched = sorted(set(edit.days) | set(edit.delete))
    items = tuple(
        (d, log.get(d), after.get(d)) for d in touched if _line(log.get(d)) != _line(after.get(d))
    )
    return Change(before=dict(log), after=after, items=items)


def _normalised(fields: Mapping[str, object]) -> dict[str, object]:
    out = dict(fields)
    if "symptoms" in out:
        out["symptoms"] = tuple(sorted(set(out["symptoms"])))  # type: ignore[arg-type]
    if "note" in out:
        out["note"] = out["note"] or None
    return out


def _line(day: Day | None) -> str | None:
    return None if day is None else day.to_line()


def _check_source(source: str) -> None:
    if source not in SOURCES and not IMPORT_SOURCE.match(source):
        raise ValueError("unknown_source")


def _parse_date(raw: object) -> date:
    if not isinstance(raw, str) or not _DATE.match(raw):
        raise ValueError("bad_date")
    return date.fromisoformat(raw)
