"""Cycle logic: pure functions of the log and the date. Nothing about the cycle is stored.

`bot_day` turns a moment into the day the bot talks about, `predict` learns the next start,
`phase` says where she is, and `queue` lists the dates the check-in card still wants.
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from supercycletracker.records import Day, starts

DAY_ENDS_AT = timedelta(hours=5)  # a reply at 00:30 still belongs to the evening before
MIN_LENGTH, MAX_LENGTH = 20, 45  # shorter is spotting logged as flow, longer a month not logged
RECENT = 6
MIN_LENGTHS = 3
DEFAULT_LENGTH = 28
DEFAULT_PERIOD_DAYS = 5
MIN_RUN = 3  # a run of bleed days shorter than this is a partly logged period
PERIOD_MAX_DAY = 7  # the bot asks daily up to day 7
CATCHUP_DAY = 8  # on day 8 it asks only for the gaps, once
MISSING_BACK = 3  # missing days older than this are never asked about
KINDS = ("no_history", "quiet", "waiting", "late", "period")  # every Phase.kind


def bot_day(now: datetime, tz: tzinfo | str) -> date:
    """The day the bot talks about at `now`: the local date, with the day ending at 05:00."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = now.astimezone(zone) if now.tzinfo else now.replace(tzinfo=zone)
    return (local.replace(tzinfo=None) - DAY_ENDS_AT).date()


@dataclass(frozen=True)
class Prediction:
    last_start: date
    lengths: tuple[int, ...]  # the recent usable cycle lengths, oldest first (at most 6)
    median: int  # the cycle length the prediction uses; 28 with fewer than 3 lengths
    expected: date
    window_start: date
    window_end: date
    period_days: int


@dataclass(frozen=True)
class Phase:
    kind: str  # no_history | quiet | waiting | late | period
    day: int | None  # period day, 1-based, while the period is open
    start: date | None  # the latest start on or before today
    missing: tuple[date, ...]  # period days the bot still wants, oldest first
    prediction: Prediction | None

    def __post_init__(self):
        if self.kind not in KINDS:  # a new kind needs its phase_ locale key: add it to KINDS
            raise ValueError("unknown_phase_kind")


def _upto(log: Mapping[date, Day], today: date) -> dict[date, Day]:
    return {d: day for d, day in log.items() if d <= today}


def period_days(log: Mapping[date, Day]) -> int:
    """The median length of the runs of 3 or more consecutive bleed days; 5 without one."""
    runs: list[int] = []
    previous: date | None = None
    for d in sorted(d for d, day in log.items() if day.bleed):
        if previous is not None and (d - previous).days == 1:
            runs[-1] += 1
        else:
            runs.append(1)
        previous = d
    lengths = [r for r in runs if r >= MIN_RUN]
    return round(statistics.median(lengths)) if lengths else DEFAULT_PERIOD_DAYS


def predict(log: Mapping[date, Day], today: date) -> Prediction | None:
    """The next start from the starts on or before `today`; None with no start at all."""
    past = _upto(log, today)
    first_days = starts(past)
    if not first_days:
        return None
    last = first_days[-1]
    usable = [
        (b - a).days
        for a, b in itertools.pairwise(first_days)
        if MIN_LENGTH <= (b - a).days <= MAX_LENGTH
    ]
    recent = tuple(usable[-RECENT:])
    if len(recent) < MIN_LENGTHS:
        median = DEFAULT_LENGTH
        expected = last + timedelta(days=median)
        window = (expected - timedelta(days=3), expected + timedelta(days=3))
    else:
        median = round(statistics.median(recent))  # Python's round: a half goes to the even
        expected = last + timedelta(days=median)
        window = (
            min(last + timedelta(days=min(recent)), expected - timedelta(days=2)),
            max(last + timedelta(days=max(recent)), expected + timedelta(days=2)),
        )
    return Prediction(
        last_start=last,
        lengths=recent,
        median=median,
        expected=expected,
        window_start=window[0],
        window_end=window[1],
        period_days=period_days(past),
    )


def phase(log: Mapping[date, Day], today: date, skipped: Collection[date] = ()) -> Phase:
    """Where she is on bot day `today`. `skipped` are the days she answered «No me acuerdo»."""
    past = _upto(log, today)
    first_days = starts(past)
    prediction = predict(past, today)
    if not first_days or prediction is None:
        return Phase("no_history", None, None, (), None)
    start = first_days[-1]
    n = (today - start).days + 1

    # The latest record with a flow in [start, today] says whether the period is open; a `none`
    # closes it, a bleed day after a `none` reopens it.
    flows = sorted((d, day) for d, day in past.items() if d >= start and day.flow is not None)
    latest_date, latest = flows[-1]
    closing = latest_date if not latest.bleed else None

    missing: tuple[date, ...] = ()
    if n <= CATCHUP_DAY:
        first = max(start, today - timedelta(days=MISSING_BACK))
        missing = tuple(
            d
            for d in _dates(first, today - timedelta(days=1))
            if (d not in past or past[d].flow is None)
            and d not in skipped
            and (closing is None or d < closing)
        )

    is_open = closing is None and (n <= PERIOD_MAX_DAY or (n == CATCHUP_DAY and bool(missing)))
    if is_open:
        return Phase("period", n, start, missing, prediction)
    if prediction.window_start <= today <= prediction.window_end:
        kind = "waiting"
    elif today > prediction.window_end:
        kind = "late"
    else:
        kind = "quiet"
    return Phase(kind, None, start, missing, prediction)


def queue(ph: Phase, log: Mapping[date, Day], today: date) -> tuple[date, ...]:
    """The dates the check-in card still wants: missing days oldest first, then today while
    today has no flow. On day 8 (the catch-up day) only the missing days. Outside an open period
    nothing: a closed period is not asked about, even for gaps before its closing `none`."""
    if ph.kind != "period":
        return ()
    dates = list(ph.missing)
    if ph.day is not None and ph.day <= PERIOD_MAX_DAY:
        day = log.get(today)
        if day is None or day.flow is None:
            dates.append(today)
    return tuple(dates)


def _dates(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]
