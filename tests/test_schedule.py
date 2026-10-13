"""The scheduled ask: once per bot day in `[ask_at, ask_at + 2 h)`, only when the phase wants a card.

The seed log (conftest) has fictional periods from 2027-01-04, 02-01 and 03-01 (03-01..03-03 logged,
nothing closing it): fewer than three lengths, so the next start is expected on 03-29 with the
window 03-26..04-01.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import Clock, FakeTelegram

from supercycletracker import cycle, records
from supercycletracker.bot import Bot
from supercycletracker.records import Day

MADRID = ZoneInfo("Europe/Madrid")
GROUP = -100123

PROFILE = """\
name: Lucía
timezone: Europe/Madrid
language: es
ask_at: "{ask_at}"
telegram:
  chat_id: -100123
  user_id: 111
  username: lucia_example
  admins: {{222: Marta}}
"""


def at(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=MADRID)


def periods(*spans) -> dict:
    """`(first day, bleed days)` pairs as a log."""
    log = {}
    for first, n in spans:
        for i in range(n):
            d = first + timedelta(days=i)
            log[d] = Day(date=d, source="telegram", flow="medium")
    return records.derive_starts(log)


@pytest.fixture
def make_bot(data_repo):
    """A bot whose git sync is a no-op (the repo tests cover it), so a long run stays fast."""
    bots = []

    def make(now: datetime, ask_at="21:00", log=None):
        (data_repo.data / "cycle" / "profile.yaml").write_text(
            PROFILE.format(ask_at=ask_at), encoding="utf-8"
        )
        if log is not None:
            records.save(data_repo.log, log)
        bot = Bot(data_repo.data, FakeTelegram(), now=Clock(now))
        bot.repo.flush = lambda force=False: "idle"
        bots.append(bot)
        return bot

    yield make
    for bot in bots:
        bot.close()


def cards(bot) -> list[dict]:
    return bot.tg.of("sendMessage")


def tick_at(bot, when: datetime) -> None:
    bot.now.at = when
    bot.tick()


def test_quiet_before_the_window_asks_nothing(make_bot):
    bot = make_bot(at(date(2027, 3, 20), 21))
    for hour in range(5, 24):
        tick_at(bot, at(date(2027, 3, 20), hour))
    assert cards(bot) == []


def test_the_ask_comes_at_ask_at_not_before_not_twice(make_bot):
    d = date(2027, 3, 27)  # waiting
    bot = make_bot(at(d, 20, 59))
    tick_at(bot, at(d, 20, 59))
    assert cards(bot) == []
    tick_at(bot, at(d, 21, 0))
    assert len(cards(bot)) == 1
    assert cards(bot)[0]["text"].startswith("@lucia_example, por tus últimos ciclos")
    assert cards(bot)[0]["chat_id"] == GROUP
    tick_at(bot, at(d, 21, 30))
    tick_at(bot, at(d, 22, 59))
    assert len(cards(bot)) == 1
    assert bot.kv.get("last_ask") == "2027-03-27"


def test_never_after_ask_at_plus_two_hours(make_bot):
    d = date(2027, 3, 28)
    bot = make_bot(at(d, 23))
    tick_at(bot, at(d, 23, 0))
    tick_at(bot, at(d + timedelta(days=1), 3, 0))
    assert cards(bot) == []
    tick_at(bot, at(d + timedelta(days=1), 22, 59))  # positive control: the next day's window
    assert len(cards(bot)) == 1


@pytest.mark.parametrize(
    ("when", "asks"),
    [
        (at(date(2027, 3, 27), 22, 59), False),
        (at(date(2027, 3, 27), 23, 0), True),
        (at(date(2027, 3, 28), 0, 30), True),  # still bot day 03-27, before 01:00
        (at(date(2027, 3, 28), 1, 0), False),
    ],
)
def test_ask_at_23_across_midnight(make_bot, when, asks):
    bot = make_bot(when, ask_at="23:00")
    bot.tick()
    assert len(cards(bot)) == (1 if asks else 0)
    if asks:
        assert bot.kv.get("last_ask") == "2027-03-27"


def test_no_ask_when_today_already_has_a_flow(make_bot, data_repo):
    d = date(2027, 3, 27)
    log = records.load(data_repo.log)
    log[d] = Day(date=d, source="telegram", flow="none")  # waiting, today answered «no»
    bot = make_bot(at(d, 21), log=records.derive_starts(log))
    bot.tick()
    assert cards(bot) == []

    log[d] = Day(date=d, source="telegram", flow="medium")  # day 1 already answered
    records.save(data_repo.log, records.derive_starts(log))
    assert cycle.phase(records.load(data_repo.log), d).kind == "period"
    bot.tick()
    assert cards(bot) == []


def test_a_restart_on_the_same_day_asks_no_second_time(make_bot, data_repo):
    d = date(2027, 3, 27)
    bot = make_bot(at(d, 21))
    bot.tick()
    bot.close()
    again = Bot(data_repo.data, FakeTelegram(), now=Clock(at(d, 21, 30)))
    again.repo.flush = lambda force=False: "idle"
    try:
        again.tick()
        assert again.tg.of("sendMessage") == []
    finally:
        again.close()
    assert len(cards(bot)) == 1


def test_late_asks_every_day(make_bot):
    first = date(2027, 4, 2)  # the window closed on 04-01
    bot = make_bot(at(first, 21))
    for i in range(5):
        tick_at(bot, at(first + timedelta(days=i), 21, 5))
    texts = [c["text"] for c in cards(bot)]
    assert len(texts) == 5
    assert all(t.startswith("@lucia_example, ¿alguna novedad?") for t in texts)


def test_sixty_frozen_days_never_ask_twice_on_one_bot_day(make_bot):
    """No answer for 60 days from period day 4: one card per bot day at most, none while quiet,
    and the ask keeps coming daily once the period is late."""
    start = at(date(2027, 3, 4), 0)
    bot = make_bot(start)
    per_day: dict[date, int] = {}
    for hour in range(60 * 24):
        now = start + timedelta(hours=hour)
        before = len(cards(bot))
        tick_at(bot, now)
        sent = len(cards(bot)) - before
        if sent:
            day = cycle.bot_day(now, "Europe/Madrid")
            per_day[day] = per_day.get(day, 0) + sent
    assert max(per_day.values()) == 1
    asked = sorted(per_day)
    # period days 4..7 and the day-8 catch-up, then nothing until the window opens on 03-26
    assert asked[:5] == [date(2027, 3, 4) + timedelta(days=i) for i in range(5)]
    assert asked[5] == date(2027, 3, 26)
    last = date(2027, 5, 2)
    assert asked[5:] == [date(2027, 3, 26) + timedelta(days=i) for i in range(38)]
    assert asked[-1] == last
    assert len(cards(bot)) == 43


def test_missing_days_older_than_three_are_never_asked(make_bot):
    log = periods((date(2027, 1, 4), 5), (date(2027, 2, 1), 5), (date(2027, 3, 1), 1))
    d = date(2027, 3, 6)  # day 6: 03-02 is four days back
    bot = make_bot(at(d, 21), log=log)
    bot.tick()
    card = cards(bot)[0]
    assert card["text"] == (
        "@lucia_example, se nos pasaron unos días. Vamos uno a uno: ¿cómo fue el "
        "miércoles 3 de marzo?"
    )
    data = [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert all(x.split(":")[1] == "20270303" for x in data)
    ph = cycle.phase(records.load(bot.repo.log_path), d)
    assert cycle.queue(ph, records.load(bot.repo.log_path), d) == (
        date(2027, 3, 3),
        date(2027, 3, 4),
        date(2027, 3, 5),
        d,
    )


def test_day_eight_asks_the_gaps_once_with_the_catch_up_wording(make_bot):
    log = periods((date(2027, 1, 4), 5), (date(2027, 2, 1), 5), (date(2027, 3, 1), 4))
    d = date(2027, 3, 8)  # day 8, 03-05..03-07 missing
    bot = make_bot(at(d, 21), log=log)
    bot.tick()
    tick_at(bot, at(d, 22))
    assert len(cards(bot)) == 1
    card = cards(bot)[0]
    assert card["text"] == (
        "@lucia_example, para cerrar esta regla me falta el viernes 5 de marzo. "
        "¿Te acuerdas más o menos de cómo fue?"
    )
    labels = [b["text"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert "No me acuerdo" in labels
    tick_at(bot, at(d + timedelta(days=1), 21))  # day 9: the period is over
    assert len(cards(bot)) == 1


def test_day_eight_with_nothing_missing_asks_nothing(make_bot):
    log = periods((date(2027, 1, 4), 5), (date(2027, 2, 1), 5), (date(2027, 3, 1), 7))
    bot = make_bot(at(date(2027, 3, 8), 21), log=log)
    bot.tick()
    assert cards(bot) == []


def test_no_chat_no_ask(make_bot, data_repo):
    bot = make_bot(at(date(2027, 3, 27), 21))
    (data_repo.data / "cycle" / "profile.yaml").write_text("name: Lucía\n", encoding="utf-8")
    bare = Bot(data_repo.data, FakeTelegram(), now=Clock(at(date(2027, 3, 27), 21)))
    bare.repo.flush = lambda force=False: "idle"
    try:
        bare.tick()
        assert bare.tg.of("sendMessage") == []
    finally:
        bare.close()
    bot.tick()  # positive control: the same day with a chat
    assert len(cards(bot)) == 1


def test_a_card_telegram_refuses_is_tried_again_on_the_next_tick(make_bot):
    d = date(2027, 3, 27)
    bot = make_bot(at(d, 21))
    bot.tg.fail_next("sendMessage", 502, "Bad Gateway")
    bot.tick()
    assert bot.kv.get("last_ask") is None
    tick_at(bot, at(d, 21, 1))
    assert bot.kv.get("last_ask") == "2027-03-27"
    assert len(cards(bot)) == 2  # the refused call and the one that went


def test_a_tick_syncs_git_and_touches_the_heartbeat(make_bot, tmp_path):
    bot = make_bot(at(date(2027, 3, 20), 12))
    calls = []
    bot.repo.flush = lambda force=False: calls.append(force) or "idle"
    bot.alive = tmp_path / "alive"
    bot.tick()
    assert calls == [False]
    assert bot.alive.exists()
