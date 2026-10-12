import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from supercycletracker import cycle, records
from supercycletracker.cycle import bot_day, phase, predict, queue
from supercycletracker.records import Day

FIXTURE = Path(__file__).parent / "fixtures" / "log_fictional.jsonl"
MADRID = ZoneInfo("Europe/Madrid")


def d(s: str) -> date:
    return date.fromisoformat(s)


def period(first: str, flows: list[str]) -> dict[date, Day]:
    start = d(first)
    return {
        start + timedelta(days=i): Day(date=start + timedelta(days=i), source="telegram", flow=f)
        for i, f in enumerate(flows)
    }


def history(first: str, lengths: list[int], flows=("medium", "medium", "light", "light")):
    log: dict[date, Day] = {}
    start = d(first)
    for length in [*lengths, 0]:
        log |= period(start.isoformat(), list(flows))
        start += timedelta(days=length)
    return records.derive_starts(log)


def with_days(log, *items):
    out = dict(log)
    for s, flow in items:
        out[d(s)] = Day(date=d(s), source="telegram", flow=flow)
    return records.derive_starts(out)


# --- bot day -------------------------------------------------------------------------------


def test_bot_day_ends_at_five():
    assert bot_day(datetime(2027, 2, 3, 4, 59, tzinfo=MADRID), MADRID) == d("2027-02-02")
    assert bot_day(datetime(2027, 2, 3, 5, 1, tzinfo=MADRID), MADRID) == d("2027-02-03")
    assert bot_day(datetime(2027, 2, 3, 0, 30, tzinfo=MADRID), "Europe/Madrid") == d("2027-02-02")


def test_bot_day_on_the_spring_forward_day():
    # 2026-03-29: 02:00 CET becomes 03:00 CEST (UTC+2)
    utc = UTC
    assert bot_day(datetime(2026, 3, 29, 2, 59, tzinfo=utc), MADRID) == d("2026-03-28")  # 04:59
    assert bot_day(datetime(2026, 3, 29, 3, 1, tzinfo=utc), MADRID) == d("2026-03-29")  # 05:01
    assert bot_day(datetime(2026, 3, 29, 19, 0, tzinfo=utc), MADRID) == d("2026-03-29")  # 21:00


def test_bot_day_on_the_fall_back_day():
    # 2026-10-25: 03:00 CEST becomes 02:00 CET (UTC+1)
    utc = UTC
    assert bot_day(datetime(2026, 10, 25, 0, 30, tzinfo=utc), MADRID) == d("2026-10-24")  # 02:30
    assert bot_day(datetime(2026, 10, 25, 1, 30, tzinfo=utc), MADRID) == d("2026-10-24")  # 02:30
    assert bot_day(datetime(2026, 10, 25, 3, 59, tzinfo=utc), MADRID) == d("2026-10-24")  # 04:59
    assert bot_day(datetime(2026, 10, 25, 4, 1, tzinfo=utc), MADRID) == d("2026-10-25")  # 05:01
    assert bot_day(datetime(2026, 10, 25, 20, 0, tzinfo=utc), MADRID) == d("2026-10-25")  # 21:00


# --- prediction ----------------------------------------------------------------------------


def test_median_and_window_on_the_fictional_log():
    log = records.load(FIXTURE)
    p = predict(log, d("2025-09-10"))
    assert p is not None
    assert p.last_start == d("2025-08-28")
    assert p.lengths == (32, 28, 34, 29, 27, 31)  # the last six
    assert p.median == 30
    assert p.expected == d("2025-09-27")
    assert (p.window_start, p.window_end) == (d("2025-09-24"), d("2025-10-01"))
    assert p.period_days == 4


def test_outlier_lengths_are_dropped():
    log = history("2027-01-01", [30, 19, 46, 29, 31])
    starts = records.starts(log)
    assert [(b - a).days for a, b in itertools.pairwise(starts)] == [30, 19, 46, 29, 31]
    p = predict(log, d("2027-12-31"))
    assert p.lengths == (30, 29, 31)
    assert p.median == 30


def test_window_is_widened_to_two_days_each_side():
    p = predict(history("2027-01-01", [30, 30, 30]), d("2027-12-31"))
    assert p.median == 30
    assert (p.expected - p.window_start, p.window_end - p.expected) == (
        timedelta(days=2),
        timedelta(days=2),
    )


def test_even_count_median_rounds_half_up():
    p = predict(history("2027-01-01", [29, 30, 25, 34]), d("2027-12-31"))
    assert p.median == 30  # median 29.5
    assert p.window_start == p.last_start + timedelta(days=25)
    assert p.window_end == p.last_start + timedelta(days=34)


def test_fewer_than_three_lengths_uses_28_and_three_days_each_side():
    log = history("2027-01-01", [33, 33])
    p = predict(log, d("2027-12-31"))
    assert p.lengths == (33, 33)
    assert p.median == 28
    assert p.expected == d("2027-03-08") + timedelta(days=28)
    assert (p.window_start, p.window_end) == (
        p.expected - timedelta(days=3),
        p.expected + timedelta(days=3),
    )


def test_no_start_no_prediction():
    assert predict({}, d("2027-01-01")) is None
    only_none = {d("2027-01-01"): Day(date=d("2027-01-01"), source="admin", flow="none")}
    assert predict(only_none, d("2027-01-02")) is None


def test_records_after_today_are_ignored():
    log = history("2027-01-01", [30, 30, 30])
    assert predict(log, d("2027-02-15")).last_start == d("2027-01-31")


def test_period_length():
    log = history("2027-01-01", [30, 30], flows=("medium",) * 6)
    assert predict(log, d("2027-12-31")).period_days == 6
    # runs under three days are partly logged periods and do not count; none of them: 5
    short = history("2027-01-01", [30, 30], flows=("medium", "light"))
    assert predict(short, d("2027-12-31")).period_days == cycle.DEFAULT_PERIOD_DAYS


# --- phase ---------------------------------------------------------------------------------


def test_no_history():
    ph = phase({}, d("2027-01-01"))
    assert ph.kind == "no_history"
    assert queue(ph, {}, d("2027-01-01")) == ()
    # exit: any bleed day opens a period at once
    log = with_days({}, ("2027-01-01", "light"))
    assert phase(log, d("2027-01-01")).kind == "period"


def test_period_until_none():
    log = with_days({}, ("2027-05-01", "medium"), ("2027-05-02", "heavy"))
    ph = phase(log, d("2027-05-02"))
    assert (ph.kind, ph.day, ph.start, ph.missing) == ("period", 2, d("2027-05-01"), ())
    assert queue(ph, log, d("2027-05-02")) == ()  # today already has a flow
    assert queue(phase(log, d("2027-05-03")), log, d("2027-05-03")) == (d("2027-05-03"),)
    # exit: «Ya no sangro»
    closed = with_days(log, ("2027-05-03", "none"))
    assert phase(closed, d("2027-05-03")).kind == "quiet"
    assert phase(closed, d("2027-05-04")).kind == "quiet"
    # a bleed day after the none reopens it
    reopened = with_days(closed, ("2027-05-04", "light"))
    assert (phase(reopened, d("2027-05-04")).kind, phase(reopened, d("2027-05-04")).day) == (
        "period",
        4,
    )


def test_missing_days_oldest_first_then_today():
    log = with_days({}, ("2027-05-01", "medium"), ("2027-05-03", "light"))
    today = d("2027-05-06")
    ph = phase(log, today)
    assert ph.kind == "period" and ph.day == 6
    assert ph.missing == (d("2027-05-04"), d("2027-05-05"))  # 05-02 is more than 3 days back
    assert queue(ph, log, today) == (d("2027-05-04"), d("2027-05-05"), today)
    # a day she does not remember is never asked again
    ph = phase(log, today, skipped={d("2027-05-04")})
    assert ph.missing == (d("2027-05-05"),)


def test_missing_days_stop_at_the_closing_none():
    log = with_days({}, ("2027-05-01", "medium"), ("2027-05-04", "none"))
    ph = phase(log, d("2027-05-05"))
    assert ph.kind == "quiet"
    assert ph.missing == (d("2027-05-02"), d("2027-05-03"))
    assert queue(ph, log, d("2027-05-05")) == (d("2027-05-02"), d("2027-05-03"))


def test_a_start_back_dated_four_days_opens_day_five_with_the_gaps():
    log = with_days({}, ("2027-05-01", "medium"))
    today = d("2027-05-05")
    ph = phase(log, today)
    assert (ph.kind, ph.day) == ("period", 5)
    assert queue(ph, log, today) == (
        d("2027-05-02"),
        d("2027-05-03"),
        d("2027-05-04"),
        today,
    )


def test_day_eight_is_period_only_while_days_are_missing():
    full = with_days({}, *[(f"2027-05-0{i}", "light") for i in range(1, 8)])
    ph = phase(full, d("2027-05-07"))
    assert (ph.kind, ph.day) == ("period", 7)
    assert phase(full, d("2027-05-08")).kind == "quiet"  # nothing missing: no catch-up

    gaps = with_days({}, *[(f"2027-05-0{i}", "light") for i in range(1, 6)])
    ph = phase(gaps, d("2027-05-08"))
    assert (ph.kind, ph.day) == ("period", 8)
    assert ph.missing == (d("2027-05-06"), d("2027-05-07"))
    assert queue(ph, gaps, d("2027-05-08")) == ph.missing  # never day 8 itself
    # exit: the gaps answered or skipped
    answered = with_days(gaps, ("2027-05-06", "light"), ("2027-05-07", "none"))
    assert phase(answered, d("2027-05-08")).kind == "quiet"
    skipped = {d("2027-05-06"), d("2027-05-07")}
    assert phase(gaps, d("2027-05-08"), skipped=skipped).kind == "quiet"
    # day 9 is never a period day
    assert phase(gaps, d("2027-05-09")).kind == "quiet"


def test_quiet_waiting_late_and_their_exits():
    log = history("2027-01-01", [30, 30, 30])  # last start 2027-04-01, expected 05-01
    p = predict(log, d("2027-04-10"))
    assert (p.window_start, p.window_end) == (d("2027-04-29"), d("2027-05-03"))

    assert phase(log, d("2027-04-28")).kind == "quiet"
    assert phase(log, d("2027-04-29")).kind == "waiting"
    assert phase(log, d("2027-05-03")).kind == "waiting"
    assert phase(log, d("2027-05-04")).kind == "late"
    assert phase(log, d("2027-06-20")).kind == "late"  # no cap
    assert queue(phase(log, d("2027-05-04")), log, d("2027-05-04")) == ()

    # exit from waiting: she says it came
    came = with_days(log, ("2027-04-30", "medium"))
    ph = phase(came, d("2027-04-30"))
    assert (ph.kind, ph.day, ph.start) == ("period", 1, d("2027-04-30"))
    # exit from late: a start
    late = with_days(log, ("2027-05-05", "light"))
    assert (phase(late, d("2027-05-06")).kind, phase(late, d("2027-05-06")).day) == ("period", 2)
    # exit from quiet: an early start opens the period at once
    early = with_days(log, ("2027-04-20", "heavy"))
    assert phase(early, d("2027-04-20")).kind == "period"


def test_spotting_does_not_open_a_period():
    log = history("2027-01-01", [30, 30, 30])
    log[d("2027-04-15")] = Day(date=d("2027-04-15"), source="telegram", symptoms=("spotting",))
    assert phase(records.derive_starts(log), d("2027-04-15")).kind == "quiet"
