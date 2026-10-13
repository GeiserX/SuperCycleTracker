"""Seed the log from an Apple Health export (`export.zip` or the `export.xml` inside it).

The export is streamed with `iterparse` and every element is cleared after use, so a file of a
gigabyte runs in constant memory. Expat reads the export's inline DTD and resolves no external
entity. Days written from the chat (`telegram`, `admin`) are never overwritten; days from an earlier
import are replaced. Nothing is committed: the person reviews `git diff` and commits.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import IO
from xml.etree.ElementTree import ParseError, iterparse
from zoneinfo import ZoneInfo

from supercycletracker import records
from supercycletracker.records import Day
from supercycletracker.repo import LOG_FILE

FLOW_TYPE = "HKCategoryTypeIdentifierMenstrualFlow"
START_KEY = "HKMenstrualCycleStart"
FLOW_NAMES = {
    "Unspecified": "unspecified",
    "Light": "light",
    "Medium": "medium",
    "Heavy": "heavy",
    "None": "none",
}
FLOW_PREFIXES = ("HKCategoryValueVaginalBleeding", "HKCategoryValueMenstrualFlow")
FLOW_NUMBERS = {"1": "unspecified", "2": "light", "3": "medium", "4": "heavy", "5": "none"}
WEIGHT = {"none": 0, "unspecified": 1, "light": 2, "medium": 3, "heavy": 4}  # heaviest wins

SYMPTOM_TYPES = {
    "HKCategoryTypeIdentifier" + name: symptom
    for name, symptom in (
        ("AbdominalCramps", "abdominal_cramps"),
        ("Bloating", "bloating"),
        ("Headache", "headache"),
        ("LowerBackPain", "lower_back_pain"),
        ("PelvicPain", "pelvic_pain"),
        ("BreastPain", "breast_pain"),
        ("Nausea", "nausea"),
        ("Diarrhea", "diarrhea"),
        ("Constipation", "constipation"),
        ("Fatigue", "fatigue"),
        ("MoodChanges", "mood_changes"),
        ("Acne", "acne"),
        ("SleepChanges", "sleep_changes"),
        ("AppetiteChanges", "appetite_changes"),
        ("IntermenstrualBleeding", "spotting"),
    )
}
# Every symptom type but IntermenstrualBleeding takes a value whose integer 1 means absent
# (severity "not present", presence "not present", appetite "no change").
NO_ABSENT_VALUE = {"HKCategoryTypeIdentifierIntermenstrualBleeding"}
SKIPPED_DIRS = ("electrocardiograms", "workout-routes")


class ImportFailed(Exception):
    """The export could not be read. The message is a reason code."""


@dataclass
class Report:
    records_read: int = 0
    days_written: int = 0
    days_protected: int = 0  # imported days left alone because the chat wrote them
    starts: int = 0
    flagged: int = 0  # days the export flags as a cycle start
    agree: int = 0  # of those, the ones the 10-day rule also finds
    source: str = ""
    changed: list[date] = field(default_factory=list)

    def text(self, dry_run: bool = False) -> str:
        verb = "would write" if dry_run else "wrote"
        return (
            f"records read: {self.records_read}\n"
            f"days {verb}: {self.days_written}\n"
            f"days kept from the chat: {self.days_protected}\n"
            f"cycle starts: {self.starts}\n"
            f"export start flags: {self.flagged}, agreeing with the 10-day rule: {self.agree}\n"
            + ("dry run: nothing written" if dry_run else "review with git diff, then commit")
        )


@contextmanager
def _open(path: Path) -> Iterator[IO[bytes]]:
    """The export XML as a binary stream. A zip opens its largest `*.xml` member that is not a
    `*_cda.xml` and not under `electrocardiograms/` or `workout-routes/`: the member's name is
    localised (`export.xml`, `exportación.xml`), so it is never hard-coded."""
    if not path.is_file():
        raise ImportFailed("no_such_file")
    if not zipfile.is_zipfile(path):
        with path.open("rb") as fh:
            yield fh
        return
    with zipfile.ZipFile(path) as z:
        members = [
            m
            for m in z.infolist()
            if m.filename.lower().endswith(".xml")
            and not m.filename.lower().endswith("_cda.xml")
            and not any(part in SKIPPED_DIRS for part in m.filename.split("/")[:-1])
        ]
        if not members:
            raise ImportFailed("no_export_xml")
        member = max(members, key=lambda m: m.file_size)
        with z.open(member) as fh:
            yield fh


def _flow(value: str) -> str | None:
    if value in FLOW_NUMBERS:
        return FLOW_NUMBERS[value]
    for prefix in FLOW_PREFIXES:
        if value.startswith(prefix):
            return FLOW_NAMES.get(value.removeprefix(prefix))
    return None


def _present(kind: str, value: str) -> bool:
    if value.endswith(("NotPresent", "NoChange")):
        return False
    return not (value == "1" and kind not in NO_ABSENT_VALUE)


@dataclass
class Parsed:
    flows: dict[date, str] = field(default_factory=dict)
    symptoms: dict[date, set[str]] = field(default_factory=dict)
    flagged: set[date] = field(default_factory=set)
    records_read: int = 0
    export_date: str | None = None


def parse(path: Path, tz: str) -> Parsed:
    """Read the flow and symptom records of an export, a day per local date in `tz`."""
    zone = ZoneInfo(tz)
    out = Parsed()
    with _open(Path(path)) as fh:
        root = None
        depth = 0
        try:
            for event, elem in iterparse(fh, events=("start", "end")):
                if event == "start":
                    if root is None:
                        root = elem
                    depth += 1
                    continue
                depth -= 1
                if depth != 1:
                    continue  # a child (MetadataEntry) is read with its Record
                if elem.tag == "ExportDate":
                    out.export_date = (elem.get("value") or "")[:10]
                elif elem.tag == "Record":
                    _record(elem, zone, out)
                elem.clear()
                root.clear()
        except ParseError:
            raise ImportFailed("bad_xml") from None  # its message may quote the file
    return out


def _record(elem, zone: ZoneInfo, out: Parsed) -> None:
    kind, value = elem.get("type") or "", elem.get("value") or ""
    if kind != FLOW_TYPE and kind not in SYMPTOM_TYPES:
        return
    try:
        moment = datetime.strptime(elem.get("startDate") or "", "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return
    d = moment.astimezone(zone).date()
    out.records_read += 1
    if kind == FLOW_TYPE:
        flow = _flow(value)
        if flow is None:
            return
        if d not in out.flows or WEIGHT[flow] > WEIGHT[out.flows[d]]:
            out.flows[d] = flow
        for meta in elem.iter("MetadataEntry"):
            if meta.get("key") == START_KEY and meta.get("value") == "1":
                out.flagged.add(d)
    elif _present(kind, value):
        out.symptoms.setdefault(d, set()).add(SYMPTOM_TYPES[kind])


def merge(export: Path, data: Path, tz: str, dry_run: bool = False) -> Report:
    """Merge an export into `<data>/cycle/log.jsonl` and report counts. Chat days stay as they
    are, earlier imports are replaced, `cycle_start` is re-derived. A rerun of the same export
    writes nothing."""
    parsed = parse(export, tz)
    if not parsed.export_date:
        raise ImportFailed("no_export_date")
    source = f"apple-health-export-{parsed.export_date}"
    if not records.IMPORT_SOURCE.match(source):
        raise ImportFailed("bad_export_date")
    path = Path(data) / LOG_FILE
    try:
        current = records.load(path)
    except ValueError as e:
        raise ImportFailed(f"log: {e}") from None
    report = Report(records_read=parsed.records_read, source=source)
    new = dict(current)
    for d in sorted(set(parsed.flows) | set(parsed.symptoms)):
        old = current.get(d)
        if old is not None and old.source in records.SOURCES:
            report.days_protected += 1
            continue
        new[d] = Day(
            date=d,
            source=source,
            flow=parsed.flows.get(d),
            symptoms=tuple(sorted(parsed.symptoms.get(d, ()))),
        )
    after = records.derive_starts(new)
    report.changed = [
        d
        for d in sorted(set(current) | set(after))
        if (current[d].to_line() if d in current else None)
        != (after[d].to_line() if d in after else None)
    ]
    report.days_written = len(report.changed)
    starts = set(records.starts(after))
    report.starts = len(starts)
    report.flagged = len(parsed.flagged)
    report.agree = len(parsed.flagged & starts)
    if report.changed and not dry_run:
        records.save(path, after)
    return report
