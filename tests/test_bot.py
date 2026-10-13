import logging
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import Clock, FakeTelegram

from supercycletracker import assistant, cycle, records
from supercycletracker.bot import (
    TEXT_MAX,
    Bot,
    Interrupted,
    parse_anotar,
    parse_callback,
    split_text,
)
from supercycletracker.i18n import Translator
from supercycletracker.records import Day
from supercycletracker.telegram import TelegramError

FIXTURES = Path(__file__).parent / "fixtures"
MADRID = ZoneInfo("Europe/Madrid")
GROUP, HER, ADMIN, STRANGER, BOT_ID = -100123, 111, 222, 333, 999

PROFILE = """\
name: Lucía
birth_date: 1993-05-02
timezone: Europe/Madrid
language: es
ask_at: "21:00"
telegram:
  chat_id: -100123
  user_id: 111
  username: lucia_example
  admins: {222: Marta}
assistant:
  plugin: plugins/fake.py
  timeout_s: 5
  model: null
  instructions: ""
"""


def at(day: date, hour: int = 21, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MADRID)


@pytest.fixture
def make_bot(data_repo):
    """A bot on the fictional seed log (periods from 2027-01-04, 02-01 and 03-01, the last one
    with three bleed days and no closing day), at 21:00 on `today`."""
    bots = []

    def make(today=date(2027, 3, 4), profile=PROFILE, log=None, read_all=False):
        (data_repo.data / "cycle" / "profile.yaml").write_text(profile, encoding="utf-8")
        (data_repo.data / "plugins").mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "fake_plugin.py", data_repo.data / "plugins" / "fake.py")
        if log is not None:
            records.save(data_repo.log, records.derive_starts(log))
        tg = FakeTelegram()
        tg.me["can_read_all_group_messages"] = read_all
        bot = Bot(data_repo.data, tg, now=Clock(at(today)))
        bot.start()
        bots.append(bot)
        return bot

    yield make
    for bot in bots:
        bot.close()


def plugin(bot):
    return assistant.load_module(bot.data, "plugins/fake.py")


def tap(bot, data, user=HER, message_id=1, chat=GROUP):
    bot.handle(
        {
            "update_id": 1,
            "callback_query": {
                "id": f"cb-{data}",
                "from": {"id": user},
                "message": {"message_id": message_id, "chat": {"id": chat, "type": "supergroup"}},
                "data": data,
            },
        }
    )


_ids = iter(range(5000, 10_000))


def say(
    bot, text, user=HER, chat=GROUP, reply_to=None, entities=None, private=False, reply_from=BOT_ID
):
    message = {
        "message_id": next(_ids),
        "from": {"id": user},
        "chat": {"id": chat, "type": "private" if private else "supergroup"},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": 1,
            "from": {"id": reply_from},
            "text": reply_to,
        }
    if entities:
        message["entities"] = entities
    bot.handle({"update_id": 2, "message": message})
    bot.worker.join_idle()
    return message


def card(bot, today=None):
    sent = bot.send_card(GROUP, today or bot.today())
    return sent["message_id"]


def edits(bot):
    return bot.tg.of("editMessageText")


def toasts(bot):
    return [p.get("text") for p in bot.tg.of("answerCallbackQuery")]


def buttons(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def day(bot, d):
    return records.load(bot.repo.log_path).get(d)


def record(text, *days, delete=()):
    import json

    block = {"days": list(days), "delete": list(delete)}
    return f"{text}\n\n```record\n{json.dumps(block)}\n```"


# --- callbacks -----------------------------------------------------------------------------


def test_callback_data_is_parsed_strictly_and_fits_64_bytes():
    assert parse_callback("p:20270304:10") == ("p", date(2027, 3, 4), "10")
    assert parse_callback("d:20270304") == ("d", date(2027, 3, 4), None)
    for bad in (
        "p:20270304:11",
        "f:20270304:spotting",
        "s:20270304:fever",
        "d:2027030",
        "x:1",
        "d:20270231",
        "y:20270304",
        None,
        "pick:20270304:x",
    ):
        with pytest.raises(ValueError):
            parse_callback(bad)
    assert len(b"s:20270304:appetite_changes") <= 64


def test_three_taps_walk_one_message_and_record_the_day(make_bot):
    bot = make_bot(today=date(2027, 3, 29))  # inside the window, no period open
    mid = card(bot)
    first = bot.tg.of("sendMessage")[0]
    assert first["text"].startswith("@lucia_example, por tus últimos ciclos")
    assert buttons(first["reply_markup"])[:3] == [
        "y:20270329:light",
        "y:20270329:medium",
        "y:20270329:heavy",
    ]
    tap(bot, "y:20270329:medium", message_id=mid)
    tap(bot, "p:20270329:4", message_id=mid)
    tap(bot, "s:20270329:abdominal_cramps", message_id=mid)
    tap(bot, "d:20270329", message_id=mid)
    assert len(bot.tg.of("sendMessage")) == 1  # the card only: every step edited it
    steps = edits(bot)
    assert {e["message_id"] for e in steps} == {mid}
    assert steps[0]["text"].startswith("Lunes 29 de marzo: normal. ¿Y el dolor?")
    assert steps[1]["text"].startswith("Lunes 29 de marzo: normal, dolor 4. ¿Algo más?")
    assert "✓ calambres" in [
        b["text"] for row in steps[2]["reply_markup"]["inline_keyboard"] for b in row
    ]
    assert steps[-1]["text"] == (
        "Hecho: lunes 29 de marzo, día 1. Normal, dolor 4, calambres. "
        "Mañana a las 21:00 te vuelvo a preguntar."
    )
    assert buttons(steps[-1]["reply_markup"]) == ["fix:20270329", "more:20270329"]
    got = day(bot, date(2027, 3, 29))
    assert (got.flow, got.pain, got.symptoms, got.cycle_start, got.source) == (
        "medium",
        4,
        ("abdominal_cramps",),
        True,
        "telegram",
    )
    assert toasts(bot)[0] == "Apuntado: normal"


def test_the_same_tap_twice_changes_nothing(make_bot):
    bot = make_bot(today=date(2027, 3, 29))
    tap(bot, "y:20270329:medium")
    before = bot.repo.log_path.read_bytes()
    tap(bot, "y:20270329:medium")
    assert bot.repo.log_path.read_bytes() == before


def test_a_stale_keyboard_records_its_own_date(make_bot):
    bot = make_bot()  # 2027-03-04, period day 4
    mid = card(bot)
    bot.now.advance(days=2)
    tap(bot, "f:20270304:heavy", message_id=mid)
    assert day(bot, date(2027, 3, 4)).flow == "heavy"
    assert day(bot, date(2027, 3, 6)) is None


def test_a_callback_older_than_90_days_gets_a_toast_and_writes_nothing(make_bot):
    bot = make_bot(today=date(2027, 6, 2))
    before = bot.repo.log_path.read_bytes()
    tap(bot, "y:20270303:heavy")  # 91 days back
    assert toasts(bot) == ["Esa tarjeta es demasiado antigua, así que no he guardado nada."]
    assert bot.repo.log_path.read_bytes() == before
    assert edits(bot) == []
    tap(bot, "y:20270304:heavy")  # 90 days back: accepted (positive control)
    assert day(bot, date(2027, 3, 4)).flow == "heavy"


def test_a_future_callback_writes_nothing(make_bot):
    bot = make_bot()
    tap(bot, "f:20270305:heavy")
    assert day(bot, date(2027, 3, 5)) is None
    assert toasts(bot) == ["Algo ha fallado, así que no he guardado nada."]  # not «too old»


def test_fix_reopens_the_flow_step_with_a_question_for_a_day_already_answered(make_bot):
    bot = make_bot()  # 2027-03-04, period day 4; 03-03 is answered
    before = bot.repo.log_path.read_bytes()
    tap(bot, "fix:20270303")
    past = edits(bot)[-1]
    assert past["text"] == (
        "@lucia_example, vamos a corregir el miércoles 3 de marzo. ¿Cómo lo apunto?"
    )
    assert buttons(past["reply_markup"])[:3] == [
        "f:20270303:light",
        "f:20270303:medium",
        "f:20270303:heavy",
    ]
    tap(bot, "fix:20270304")
    now = edits(bot)[-1]
    assert now["text"] == "@lucia_example, vamos a corregir el jueves 4 de marzo. ¿Cómo lo apunto?"
    assert "f:20270304:none" in buttons(now["reply_markup"])
    assert bot.repo.log_path.read_bytes() == before


def test_same_as_yesterday_copies_flow_pain_and_symptoms_in_one_tap(make_bot):
    log = {
        date(2027, 3, 1): Day(date(2027, 3, 1), "telegram", flow="heavy"),
        date(2027, 3, 2): Day(date(2027, 3, 2), "telegram", flow="medium"),
        date(2027, 3, 3): Day(
            date(2027, 3, 3), "telegram", flow="light", pain=3, symptoms=("headache",)
        ),
    }
    bot = make_bot(log=log)
    mid = card(bot)
    assert "f:20270304:same" in buttons(bot.tg.of("sendMessage")[0]["reply_markup"])
    tap(bot, "f:20270304:same", message_id=mid)
    got = day(bot, date(2027, 3, 4))
    assert (got.flow, got.pain, got.symptoms) == ("light", 3, ("headache",))
    assert edits(bot)[-1]["text"].startswith("Hecho: jueves 4 de marzo, día 4. Poco, dolor 3")


def test_same_as_yesterday_is_only_offered_after_a_day_with_a_flow(make_bot):
    log = {date(2027, 3, 2): Day(date(2027, 3, 2), "telegram", flow="medium")}
    bot = make_bot(log=log)
    card(bot)  # day 3, yesterday has no flow: the missing day comes first
    first = buttons(bot.tg.of("sendMessage")[0]["reply_markup"])
    assert first[0] == "f:20270303:light"
    assert "f:20270303:same" in first  # 03-02 has a flow
    assert "skip:20270303" in first and "f:20270303:none" in first


def test_the_picker_starts_an_earlier_day_and_then_asks_the_gaps(make_bot):
    bot = make_bot(today=date(2027, 3, 29))
    mid = card(bot)
    tap(bot, "pick:20270329", message_id=mid)
    picker = edits(bot)[-1]
    assert picker["text"] == "¿Qué día te bajó?"
    assert buttons(picker["reply_markup"]) == [
        *(f"y:202703{d}:unspecified" for d in range(23, 29)),
        "back:20270329",
    ]
    assert [b["text"] for b in picker["reply_markup"]["inline_keyboard"][0]] == [
        "mar 23",
        "mié 24",
        "jue 25",
    ]
    tap(bot, "y:20270325:unspecified", message_id=mid)
    assert day(bot, date(2027, 3, 25)).flow == "unspecified"
    assert day(bot, date(2027, 3, 25)).cycle_start
    assert edits(bot)[-1]["text"].endswith("¿cómo fue el jueves 25 de marzo?")
    tap(bot, "f:20270325:medium", message_id=mid)
    tap(bot, "p:20270325:2", message_id=mid)
    tap(bot, "d:20270325", message_id=mid)
    nxt = edits(bot)[-1]
    assert nxt["text"] == "Anotado el jueves 25 de marzo. Ahora, ¿cómo fue el viernes 26 de marzo?"
    assert buttons(nxt["reply_markup"])[0] == "f:20270326:light"
    tap(bot, "back:20270329", message_id=mid)  # «Volver» goes back to the waiting card
    assert buttons(edits(bot)[-1]["reply_markup"])[0] == "y:20270329:light"


def test_not_yet_stores_nothing(make_bot):
    bot = make_bot(today=date(2027, 3, 29))
    before = bot.repo.log_path.read_bytes()
    tap(bot, "wait:20270329")
    assert edits(bot)[-1]["text"] == "Vale. Mañana te vuelvo a preguntar."
    assert "reply_markup" not in edits(bot)[-1]
    assert bot.repo.log_path.read_bytes() == before


def test_i_dont_remember_skips_the_day_for_good(make_bot):
    log = {date(2027, 3, 1): Day(date(2027, 3, 1), "telegram", flow="heavy")}
    bot = make_bot(log=log, today=date(2027, 3, 3))
    card(bot)
    tap(bot, "skip:20270302")
    assert edits(bot)[-1]["text"].startswith("No pasa nada, dejo el martes 2 de marzo en blanco.")
    assert buttons(edits(bot)[-1]["reply_markup"])[0] == "f:20270303:light"
    assert bot.kv.get("skip:2027-03-02") == "1"
    assert bot.card(date(2027, 3, 3))[1]["inline_keyboard"][0][0]["callback_data"].startswith(
        "f:20270303"
    )


def test_it_stopped_closes_the_period_and_posts_the_closing_line(make_bot):
    bot = make_bot()  # 03-01..03-03 bleed, today 03-04
    mid = card(bot)
    tap(bot, "f:20270304:none", message_id=mid)
    assert day(bot, date(2027, 3, 4)).flow == "none"
    closing = [x for x in bot.tg.texts() if x.startswith("Cierro esta regla")]
    assert closing == [
        (
            "Cierro esta regla: 3 días, del 1 de marzo al 3 de marzo. La próxima, hacia el "
            "29 de marzo (entre el 26 de marzo y el 1 de abril). Hasta entonces no te molesto."
        )
    ]
    assert "Mañana" not in edits(bot)[-1]["text"]  # closed: no daily ask tomorrow


def test_a_close_undone_and_redone_posts_again(make_bot):
    bot = make_bot()
    tap(bot, "f:20270304:none")
    bot.repo.undo()
    assert day(bot, date(2027, 3, 4)) is None
    tap(bot, "f:20270304:none")
    assert len([x for x in bot.tg.texts() if x.startswith("Cierro esta regla")]) == 2


def test_day_seven_says_the_bot_stops_asking_daily(make_bot):
    first = date(2027, 3, 1)
    log = {
        first + timedelta(days=i): Day(first + timedelta(days=i), "telegram", flow="light")
        for i in range(6)
    }
    bot = make_bot(log=log, today=date(2027, 3, 7))
    tap(bot, "f:20270307:same")
    assert edits(bot)[-1]["text"].endswith(
        "Llevamos 7 días, así que dejo de preguntarte cada día. Si sigue, cuéntamelo con /hoy "
        "cuando quieras; si dura bastante más de lo habitual, merece la pena comentarlo con tu "
        "médico."
    )


def test_pain_high_goes_out_once_per_crossing(make_bot):
    bot = make_bot()

    def warnings():
        return [x for x in bot.tg.texts() if x.startswith("Un dolor de")]

    tap(bot, "p:20270304:9")
    assert warnings() == [
        (
            "Un dolor de 9 es mucho. Si no se te pasa con lo que sueles tomar, o te pasa a "
            "menudo, merece la pena comentarlo con tu médico."
        )
    ]
    tap(bot, "p:20270304:9")
    assert len(warnings()) == 1
    tap(bot, "p:20270304:10")  # the line changed, but it was already at 8 or more
    assert len(warnings()) == 1
    tap(bot, "p:20270304:3")
    tap(bot, "p:20270304:9")
    assert len(warnings()) == 2


def test_tell_me_more_sends_a_selective_force_reply_with_the_mention(make_bot):
    bot = make_bot()
    tap(bot, "more:20270304")
    prompt = bot.tg.of("sendMessage")[-1]
    assert prompt["text"] == "@lucia_example, cuéntame. Te leo."
    assert prompt["reply_markup"]["force_reply"] is True
    assert prompt["reply_markup"]["selective"] is True


def test_without_a_username_the_mention_is_an_html_text_mention(make_bot):
    bot = make_bot(profile=PROFILE.replace("  username: lucia_example\n", ""))
    tap(bot, "more:20270304")
    prompt = bot.tg.of("sendMessage")[-1]
    assert prompt["text"] == '<a href="tg://user?id=111">Lucía</a>, cuéntame. Te leo.'
    assert prompt["parse_mode"] == "HTML"


def test_a_strangers_tap_gets_a_toast_and_changes_nothing(make_bot):
    bot = make_bot()
    before = bot.repo.log_path.read_bytes()
    tap(bot, "f:20270304:heavy", user=STRANGER)
    assert toasts(bot) == ["Esto es solo para Lucía."]
    assert edits(bot) == []
    assert bot.repo.log_path.read_bytes() == before


def test_an_admin_may_tap_for_her_and_the_line_says_so(make_bot):
    bot = make_bot()
    tap(bot, "f:20270304:heavy", user=ADMIN)
    assert day(bot, date(2027, 3, 4)).source == "admin"


def test_an_unchanged_message_is_fine_and_another_edit_failure_sends_anew(make_bot):
    bot = make_bot(today=date(2027, 3, 29))
    bot.tg.fail_next("editMessageText", 400, "Bad Request: message is not modified: x")
    tap(bot, "wait:20270329")
    assert len(bot.tg.of("sendMessage")) == 0
    bot.tg.fail_next("editMessageText", 400, "Bad Request: message to edit not found")
    tap(bot, "wait:20270329")
    assert bot.tg.texts() == ["Vale. Mañana te vuelvo a preguntar."]


def test_a_malformed_callback_writes_nothing(make_bot):
    bot = make_bot()
    before = bot.repo.log_path.read_bytes()
    tap(bot, "f:20270304:spotting")
    tap(bot, "f:20270304:heavy:extra")
    assert bot.repo.log_path.read_bytes() == before
    assert edits(bot) == []


# --- free text -----------------------------------------------------------------------------

QUESTION = "@lucia_example, día 4 de la regla. ¿Cómo va hoy?"


def test_a_reply_triggers_a_turn_with_the_contract_shape(make_bot):
    bot = make_bot()
    say(bot, "ayer poco y hoy nada", reply_to=QUESTION)
    calls = plugin(bot).CALLS
    assert len(calls) == 1
    turn = calls[0]["turn"]
    assert set(turn) == {"conversation", "rules", "context", "files", "new"}
    assert turn["conversation"] is None
    assert turn["new"] == [
        {
            "role": "user",
            "content": f"Lucía: [replying to the bot: {QUESTION}] ayer poco y hoy nada",
        }
    ]
    assert turn["files"] == {"cycle/log.jsonl": bot.repo.log_path.read_text(encoding="utf-8")}
    assert "Answer in Spanish" in turn["rules"]
    assert "Bot day 2027-03-04" in turn["context"]
    assert calls[0]["settings"] == {"timeout_s": 5, "model": None}
    assert bot.tg.texts() == ["Vale."]
    assert bot.kv.get(f"conv:{GROUP}") == "conv-1"
    assert bot.kv.get_json(f"pending:{GROUP}") is None
    say(bot, "gracias", reply_to="Vale.")
    assert plugin(bot).CALLS[1]["turn"]["conversation"] == "conv-1"


def test_a_message_that_mentions_the_bot_triggers_a_turn(make_bot):
    bot = make_bot()
    say(
        bot,
        "hola @example_cycle_bot qué tal",
        entities=[{"type": "mention", "offset": 5, "length": 18}],
    )
    assert plugin(bot).CALLS[0]["turn"]["new"][0]["content"] == "Lucía: hola  qué tal"


def test_a_mention_after_an_emoji_is_cut_in_utf16(make_bot):
    bot = make_bot()
    text = "🩸 @example_cycle_bot me bajó"
    say(bot, text, entities=[{"type": "mention", "offset": 3, "length": 18}])
    assert plugin(bot).CALLS[0]["turn"]["new"][0]["content"] == "Lucía: 🩸  me bajó"


def test_the_record_is_written_and_the_confirmation_comes_from_the_change(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        record(
            "Lo he guardado todo, y también lo de la semana pasada.",
            {"date": "2027-03-04", "flow": "light", "pain": 2, "symptoms": ["headache"]},
            {"date": "2027-03-03", "flow": "light"},  # the seed's own value: unchanged
        )
    ]
    say(bot, "hoy poco, me duele la cabeza", reply_to=QUESTION)
    got = day(bot, date(2027, 3, 4))
    assert (got.flow, got.pain, got.symptoms, got.source) == ("light", 2, ("headache",), "telegram")
    texts = bot.tg.texts()
    assert texts[0] == "Lo he guardado todo, y también lo de la semana pasada."
    assert texts[1] == "Anotado:\n• jueves 4 de marzo: poco, dolor 2, dolor de cabeza"
    answer = bot.tg.of("sendMessage")[0]
    assert "parse_mode" not in answer
    assert answer["reply_parameters"]["message_id"] >= 5000


def test_a_changed_day_and_a_deleted_day_read_as_such(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        record("Corregido.", {"date": "2027-03-03", "flow": "heavy"}, delete=["2027-03-02"])
    ]
    say(bot, "el miércoles fue mucho y el martes me equivoqué", reply_to=QUESTION)
    assert bot.tg.texts()[1] == (
        "Anotado:\n• martes 2 de marzo: borrado\n• miércoles 3 de marzo: poco → mucho"
    )


def test_a_none_that_closes_the_period_says_so(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Vale.", {"date": "2027-03-04", "flow": "none"})]
    say(bot, "ya no sangro", reply_to=QUESTION)
    texts = bot.tg.texts()
    assert texts[1] == "Anotado:\n• jueves 4 de marzo: sin sangrado (cierro esta regla)"
    assert texts[2].startswith("Cierro esta regla: 3 días")


def test_a_note_alone_reads_as_a_note_line(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        record(
            "Apuntado.",
            {"date": "2027-03-03", "note": "una nota de ejemplo"},
            {"date": "2027-03-04", "note": "otra nota de ejemplo"},
        )
    ]
    say(bot, "apúntalo como nota", reply_to=QUESTION)
    assert bot.tg.texts()[1] == (
        "Anotado:\n• miércoles 3 de marzo: nota actualizada\n• jueves 4 de marzo: nota actualizada"
    )
    assert day(bot, date(2027, 3, 4)).note == "otra nota de ejemplo"


def test_a_block_that_changes_nothing_posts_no_confirmation(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Ya lo tenía.", {"date": "2027-03-03", "flow": "light"})]
    before = bot.repo.log_path.read_bytes()
    say(bot, "el miércoles poco", reply_to=QUESTION)
    assert bot.tg.texts() == ["Ya lo tenía."]
    assert bot.repo.log_path.read_bytes() == before


def test_a_future_dated_block_writes_nothing(make_bot, caplog):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Apuntado.", {"date": "2027-03-05", "flow": "heavy"})]
    before = bot.repo.log_path.read_bytes()
    with caplog.at_level(logging.INFO):
        say(bot, "mañana mucho", reply_to=QUESTION)
    assert bot.repo.log_path.read_bytes() == before
    refusal = bot.tg.of("sendMessage")[-1]
    assert refusal["text"].startswith("No he sabido anotarlo bien")
    assert buttons(refusal["reply_markup"])[0] == "f:20270304:light"
    assert "block refused: date_out_of_range" in caplog.text
    assert "2027-03-05" not in caplog.text
    assert "mañana mucho" not in caplog.text


def test_a_plugin_that_raises_gets_the_keyboard_and_the_line_stays(make_bot, caplog):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [RuntimeError("her words in an error")]
    with caplog.at_level(logging.INFO):
        msg = say(bot, "me duele", reply_to=QUESTION)
    down = bot.tg.of("sendMessage")[-1]
    assert down["text"].startswith("Ahora mismo no puedo leer mensajes escritos.")
    assert buttons(down["reply_markup"])[0] == "f:20270304:light"
    pending = bot.kv.get_json(f"pending:{GROUP}")
    assert [x["id"] for x in pending] == [msg["message_id"]]
    assert "ai turn failed: RuntimeError" in caplog.text
    assert "her words" not in caplog.text
    assert "me duele" not in caplog.text


def test_a_write_the_disk_refuses_says_nothing_was_saved(make_bot, monkeypatch):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Te lo apunto.", {"date": "2027-03-04", "flow": "heavy"})]

    def refuse(fn):
        raise OSError("disk full")

    monkeypatch.setattr(bot.repo, "write", refuse)
    say(bot, "hoy mucho", reply_to=QUESTION)
    texts = bot.tg.texts()
    assert texts[0] == "Te lo apunto."
    assert texts[1].startswith("No he sabido anotarlo bien")
    assert buttons(bot.tg.of("sendMessage")[1]["reply_markup"])[0] == "f:20270304:light"


def test_the_lines_are_removed_only_after_the_block_is_applied(make_bot, monkeypatch):
    bot = make_bot()
    mod = plugin(bot)
    block = record("Te lo apunto.", {"date": "2027-03-04", "flow": "heavy"})
    mod.REPLIES[:] = [block, block]
    write = bot.repo.write

    def crash(fn):
        raise RuntimeError("crash mid-turn")

    monkeypatch.setattr(bot.repo, "write", crash)
    msg = say(bot, "hoy mucho", reply_to=QUESTION)
    assert [x["id"] for x in bot.kv.get_json(f"pending:{GROUP}")] == [msg["message_id"]]
    assert day(bot, date(2027, 3, 4)) is None
    monkeypatch.setattr(bot.repo, "write", write)
    say(bot, "¿lo tienes?", reply_to=QUESTION)
    assert len(mod.CALLS[1]["turn"]["new"]) == 2  # her first line went again
    assert day(bot, date(2027, 3, 4)).flow == "heavy"
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_an_answer_telegram_refuses_gets_the_keyboard_and_keeps_the_lines(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Te lo apunto.", {"date": "2027-03-04", "flow": "heavy"})]
    bot.tg.fail_next("sendMessage", 400, "Bad Request: message is too long")
    msg = say(bot, "hoy mucho", reply_to=QUESTION)
    down = bot.tg.of("sendMessage")[-1]
    assert down["text"].startswith("Ahora mismo no puedo leer mensajes escritos.")
    assert buttons(down["reply_markup"])[0] == "f:20270304:light"
    assert day(bot, date(2027, 3, 4)) is None
    assert [x["id"] for x in bot.kv.get_json(f"pending:{GROUP}")] == [msg["message_id"]]


def test_a_long_answer_is_split_and_only_its_last_part_asks(make_bot):
    bot = make_bot()
    long = "\n\n".join(["Una línea de ejemplo bastante larga. " * 20] * 8) + "\n\n¿Y tú?"
    assert len(long) > TEXT_MAX
    plugin(bot).REPLIES[:] = [long]
    msg = say(bot, "cuéntame", reply_to=QUESTION)
    sent = bot.tg.of("sendMessage")
    assert len(sent) == 2
    assert all(len(p["text"].encode("utf-16-le")) // 2 <= TEXT_MAX for p in sent)
    assert "\n\n".join(p["text"] for p in sent) == long
    assert sent[0]["reply_parameters"]["message_id"] == msg["message_id"]
    assert "reply_parameters" not in sent[1] and "reply_markup" not in sent[0]
    assert sent[1]["reply_markup"] == {"force_reply": True, "selective": True}


def test_split_text_cuts_an_overlong_paragraph_in_utf16_units():
    assert split_text("hola") == ["hola"]
    parts = split_text("🩸" * 3000)  # 6000 UTF-16 units
    assert [len(p) for p in parts] == [2048, 952]
    assert "".join(parts) == "🩸" * 3000


def test_after_a_timeout_the_plugin_is_not_called_again_until_it_returns(make_bot):
    bot = make_bot(profile=PROFILE.replace("timeout_s: 5", "timeout_s: 0.2"))
    mod = plugin(bot)
    release, lock = threading.Event(), threading.Lock()
    running = {"now": 0, "peak": 0}

    def slow(turn):
        with lock:
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
        release.wait(5)
        with lock:
            running["now"] -= 1
        return {"text": "tarde", "conversation": "c", "kept": True}

    mod.REPLIES[:] = [slow, slow]
    try:
        say(bot, "uno", reply_to=QUESTION)
        say(bot, "dos", reply_to=QUESTION)
        assert len(mod.CALLS) == 1
        assert running["peak"] == 1
        assert [t for t in bot.tg.texts() if t.startswith("Ahora mismo")] and len(
            bot.tg.texts()
        ) == 2
    finally:
        release.set()
    abandoned = assistant._ABANDONED[str((bot.data / "plugins" / "fake.py").resolve())]
    abandoned.join(5)
    mod.REPLIES[:] = []
    say(bot, "tres", reply_to=QUESTION)
    assert len(mod.CALLS[-1]["turn"]["new"]) == 3
    assert bot.tg.texts()[-1] == "Vale."
    assert "tarde" not in bot.tg.texts()


def test_not_kept_writes_nothing_and_the_resend_gets_one_answer(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        {"text": record("Apuntado.", {"date": "2027-03-04", "flow": "heavy"}), "kept": False},
        {"text": "Ahora sí.", "conversation": "conv-2", "kept": True},
    ]
    before = bot.repo.log_path.read_bytes()
    say(bot, "hoy mucho", reply_to=QUESTION)
    assert bot.repo.log_path.read_bytes() == before
    assert [x for x in bot.tg.texts() if x.startswith("Ahora mismo no puedo")]
    assert "Apuntado." not in bot.tg.texts()
    say(bot, "¿me lees ahora?", reply_to=QUESTION)
    second = plugin(bot).CALLS[1]["turn"]["new"]
    assert [x["content"].rsplit("] ", 1)[1] for x in second] == ["hoy mucho", "¿me lees ahora?"]
    assert bot.tg.texts().count("Ahora sí.") == 1
    assert bot.kv.get_json(f"pending:{GROUP}") is None
    assert bot.kv.get(f"conv:{GROUP}") == "conv-2"


def test_twenty_queued_lines_are_capped_to_ten_and_the_next_turn_succeeds(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    mod.DOWN = True
    for i in range(20):
        say(bot, f"mensaje {i}", reply_to=QUESTION)
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 10
    mod.DOWN = False
    say(bot, "mensaje 20", reply_to=QUESTION)
    new = mod.CALLS[-1]["turn"]["new"]
    assert len(new) == 10
    assert new[-1]["content"].endswith("mensaje 20")
    assert new[0]["content"].endswith("mensaje 11")
    assert bot.tg.texts()[-1] == "Vale."
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_pending_lines_older_than_seven_days_fall_off(make_bot):
    bot = make_bot()
    plugin(bot).DOWN = True
    say(bot, "viejo", reply_to=QUESTION)
    bot.now.advance(days=8)
    say(bot, "nuevo", reply_to=QUESTION)
    pending = bot.kv.get_json(f"pending:{GROUP}")
    assert [x["content"].rsplit("] ", 1)[1] for x in pending] == ["nuevo"]


def test_a_message_arriving_mid_turn_is_not_dropped(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    entered, release = threading.Event(), threading.Event()

    def slow(turn):
        entered.set()
        release.wait(5)
        return {"text": "primera", "conversation": "c", "kept": True}

    mod.REPLIES[:] = [slow, {"text": "segunda", "conversation": "c", "kept": True}]
    bot.handle(
        {
            "message": {
                "message_id": 1,
                "from": {"id": HER},
                "text": "uno",
                "chat": {"id": GROUP, "type": "supergroup"},
                "reply_to_message": {"from": {"id": BOT_ID}, "text": "x"},
            }
        }
    )
    assert entered.wait(5)
    bot.handle(
        {
            "message": {
                "message_id": 2,
                "from": {"id": HER},
                "text": "dos",
                "chat": {"id": GROUP, "type": "supergroup"},
                "reply_to_message": {"from": {"id": BOT_ID}, "text": "x"},
            }
        }
    )
    release.set()
    bot.worker.join_idle()
    assert [c["turn"]["new"][0]["content"] for c in mod.CALLS] == [
        "Lucía: [replying to the bot: x] uno",
        "Lucía: [replying to the bot: x] dos",
    ]
    assert len(mod.CALLS[1]["turn"]["new"]) == 1
    assert bot.tg.texts() == ["primera", "segunda"]


def test_an_answer_ending_in_a_question_carries_force_reply(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["Vaya. ¿Te duele más que otras veces?", "Vale."]
    msg = say(bot, "me duele", reply_to=QUESTION)
    answer = bot.tg.of("sendMessage")[0]
    assert answer["reply_markup"] == {"force_reply": True, "selective": True}
    assert answer["reply_parameters"] == {
        "message_id": msg["message_id"],
        "allow_sending_without_reply": True,
    }
    say(bot, "sí", reply_to="Vaya. ¿Te duele más que otras veces?")
    assert "reply_markup" not in bot.tg.of("sendMessage")[1]


def test_plain_group_text_follows_the_privacy_setting(make_bot):
    bot = make_bot()  # privacy on
    say(bot, "me ha bajado hoy")
    say(bot, "¿cenamos fuera?", user=ADMIN)
    assert plugin(bot).CALLS == []
    assert bot.tg.texts() == []


def test_with_privacy_off_her_plain_text_is_answered_and_an_admins_is_not(make_bot):
    bot = make_bot(read_all=True)
    say(bot, "¿cenamos fuera?", user=ADMIN)
    assert plugin(bot).CALLS == []
    say(bot, "me ha bajado hoy")
    assert plugin(bot).CALLS[0]["turn"]["new"][0]["content"] == "Lucía: me ha bajado hoy"


def test_with_privacy_off_an_admins_reply_to_her_is_not_for_the_bot(make_bot):
    bot = make_bot(read_all=True)
    say(bot, "¿te traigo algo?", user=ADMIN, reply_to="me duele", reply_from=HER)
    assert plugin(bot).CALLS == []
    assert bot.kv.get_json(f"pending:{GROUP}") is None
    assert bot.tg.texts() == []
    say(bot, "¿te traigo algo?", user=ADMIN, reply_to=QUESTION)  # positive control
    assert len(plugin(bot).CALLS) == 1


def test_strangers_and_other_chats_get_no_answer(make_bot):
    bot = make_bot(read_all=True)
    say(bot, "hola", user=STRANGER, reply_to=QUESTION)
    say(bot, "hola", chat=-100999, reply_to=QUESTION)
    say(bot, "hola", user=STRANGER, chat=STRANGER, private=True)
    assert plugin(bot).CALLS == []
    assert bot.tg.texts() == []


def test_a_private_chat_with_her_or_an_admin_is_answered(make_bot):
    bot = make_bot()
    say(bot, "hola", chat=HER, private=True)
    say(bot, "ayer tuvo poco", user=ADMIN, chat=ADMIN, private=True)
    assert [c["turn"]["new"][0]["content"] for c in plugin(bot).CALLS] == [
        "Lucía: hola",
        "Marta: ayer tuvo poco",
    ]


def test_an_admins_block_is_written_as_admin(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Apuntado.", {"date": "2027-03-04", "flow": "heavy"})]
    say(bot, "hoy ha tenido mucho", user=ADMIN, reply_to=QUESTION)
    assert day(bot, date(2027, 3, 4)).source == "admin"


def test_hoy_with_text_goes_to_the_assistant_and_without_it_sends_the_card(make_bot):
    bot = make_bot()
    say(bot, "/hoy@example_cycle_bot poco y con calambres")
    assert plugin(bot).CALLS[0]["turn"]["new"][0]["content"] == "Lucía: poco y con calambres"
    say(bot, "/hoy")
    assert buttons(bot.tg.of("sendMessage")[-1]["reply_markup"])[0] == "f:20270304:light"
    calls = len(bot.tg.calls)
    say(bot, "/hoy@another_bot hola")
    assert len(bot.tg.calls) == calls


def test_without_an_assistant_free_text_gets_no_ai_and_the_keyboard(make_bot):
    profile = PROFILE.split("assistant:")[0]
    bot = make_bot(profile=profile)
    say(bot, "hoy poco", reply_to=QUESTION)
    answer = bot.tg.of("sendMessage")[-1]
    assert answer["text"] == (
        "Sin el asistente no entiendo mensajes escritos. Puedes anotarlo con los botones:"
    )
    assert buttons(answer["reply_markup"])[0] == "f:20270304:light"
    assert bot.kv.get_json(f"pending:{GROUP}") is None


# --- commands ------------------------------------------------------------------------------


def test_the_menu_is_set_once_with_her_five_commands(make_bot):
    bot = make_bot()
    bot.set_commands()
    (call,) = bot.tg.of("setMyCommands")
    assert [c["command"] for c in call["commands"]] == [
        "hoy",
        "resumen",
        "anotar",
        "deshacer",
        "ayuda",
    ]
    assert "scope" not in call  # the default scope
    assert all(c["description"] for c in call["commands"])


def test_anotar_writes_a_day_without_the_assistant(make_bot):
    bot = make_bot()  # 2027-03-04
    say(bot, "/anotar ayer mucho 6 calambres")
    got = day(bot, date(2027, 3, 3))
    assert (got.flow, got.pain, got.symptoms, got.source) == (
        "heavy",
        6,
        ("abdominal_cramps",),
        "telegram",
    )
    assert bot.tg.texts()[-1] == (
        "Anotado sin el asistente: miércoles 3 de marzo, mucho, dolor 6, calambres. "
        "Si no es así, /deshacer."
    )
    assert plugin(bot).CALLS == []


def test_anotar_by_an_admin_is_written_as_admin_and_the_red_flag_still_comes(make_bot):
    bot = make_bot()
    say(bot, "/log@example_cycle_bot hoy poco dolor 9", user=ADMIN)
    got = day(bot, date(2027, 3, 4))
    assert (got.flow, got.pain, got.source) == ("light", 9, "admin")
    assert bot.tg.texts()[-1].startswith("Un dolor de 9 es mucho.")


def test_anotar_refuses_what_it_does_not_understand(make_bot):
    bot = make_bot()
    before = bot.repo.log_path.read_bytes()
    for text in (
        "/anotar",
        "/anotar ayer",  # nothing to note
        "/anotar mañana mucho",
        "/anotar ayer mucho 11",
        "/anotar ayer mucho zanahorias",
        "/anotar ayer dolor",  # three symptoms start with «dolor»
        "/anotar 2026-11-01 poco",  # more than 90 days back
        "/anotar 31/2 poco",
    ):
        say(bot, text)
    assert bot.repo.log_path.read_bytes() == before
    usage = "Uso: /anotar &lt;cuándo&gt; &lt;cuánto&gt; [dolor 0-10] [síntomas…]."
    assert all(t.startswith(usage) for t in bot.tg.texts())
    assert len(bot.tg.texts()) == 8


def test_anotar_understands_weekdays_dates_and_labels_by_prefix():
    t = Translator("es")
    today = date(2027, 3, 4)  # a Thursday
    assert parse_anotar("hoy nada", t, today) == (today, {"flow": "none"})
    assert parse_anotar("anteayer normal", t, today)[0] == date(2027, 3, 2)
    assert parse_anotar("miercoles poco", t, today)[0] == date(2027, 3, 3)
    assert parse_anotar("jueves poco", t, today)[0] == date(2027, 2, 25)  # the past one
    assert parse_anotar("lun poco", t, today)[0] == date(2027, 3, 1)
    assert parse_anotar("28/2 poco", t, today)[0] == date(2027, 2, 28)
    assert parse_anotar("5/3 poco", t, date(2027, 3, 10))[0] == date(2027, 3, 5)
    assert parse_anotar("20/12 poco", t, date(2027, 1, 10))[0] == date(2026, 12, 20)
    assert parse_anotar("2027-03-01 poco", t, today)[0] == date(2027, 3, 1)
    d, fields = parse_anotar("Ayer Poquito 3 dolor de cabeza hinch nauseas", t, today)
    assert (d, fields) == (
        date(2027, 3, 3),
        {"flow": "light", "pain": 3, "symptoms": ("bloating", "headache", "nausea")},
    )
    assert parse_anotar("hoy dolor lumbar", t, today)[1] == {"symptoms": ("lower_back_pain",)}
    en = Translator("en")
    assert parse_anotar("day before yesterday heavy pain 7", en, today) == (
        date(2027, 3, 2),
        {"flow": "heavy", "pain": 7},
    )


def test_deshacer_restores_the_batch_and_then_has_nothing_to_undo(make_bot):
    bot = make_bot(today=date(2027, 3, 29))
    before = bot.repo.log_path.read_bytes()
    tap(bot, "y:20270329:medium")
    tap(bot, "p:20270329:4")
    say(bot, "/deshacer")
    assert bot.repo.log_path.read_bytes() == before
    assert bot.tg.texts()[-1] == "Deshecho: lunes 29 de marzo."
    say(bot, "/undo")
    assert bot.tg.texts()[-1] == "No hay nada que deshacer."


def test_resumen_on_the_fictional_log(make_bot):
    log = records.load(FIXTURES / "log_fictional.jsonl")
    bot = make_bot(today=date(2025, 9, 10), log=log)
    say(bot, "/resumen")
    assert bot.tg.texts()[-1] == (
        "Tu ciclo, Lucía:\n"
        "• Última regla: 28 de agosto, 5 día(s) apuntado(s).\n"
        "• Ciclos recientes: 32, 28, 34, 29, 27, 31 días (lo normal en ti: 30).\n"
        "• Duración habitual: 4 días.\n"
        "• Próxima: hacia el 27 de septiembre (entre el 24 de septiembre y el 1 de octubre).\n"
        "• Lo que más anotas: calambres, dolor de cabeza, dolor lumbar."
    )


def test_resumen_with_too_few_cycles_leaves_the_lengths_out(make_bot):
    bot = make_bot()  # two lengths in the seed
    say(bot, "/summary")
    text = bot.tg.texts()[-1]
    assert "Ciclos recientes" not in text
    assert "• Próxima: hacia el 29 de marzo" in text
    bot2 = make_bot(log={})
    say(bot2, "/resumen")
    assert bot2.tg.texts()[-1] == "Aún no tengo datos suficientes."


ESTADO_CASES = [
    ("period", date(2027, 3, 4), None, "regla, día 4"),
    ("quiet", date(2027, 3, 15), None, "tranquila hasta el 26 de marzo"),
    ("waiting", date(2027, 3, 27), None, "esperando (del 26 de marzo al 1 de abril)"),
    ("late", date(2027, 4, 5), None, "con retraso desde el 1 de abril"),
    ("no_history", date(2027, 3, 4), {}, "sin historial"),
]


@pytest.mark.parametrize(("kind", "today", "log", "phase"), ESTADO_CASES)
def test_estado_renders_every_phase_kind_through_its_locale_key(make_bot, kind, today, log, phase):
    bot = make_bot(today=today, log=log)
    assert bot.phase(bot.repo.load(), today).kind == kind
    say(bot, "/estado", user=ADMIN)
    assert bot.tg.texts()[-1] == (
        f"Fase: {phase}. Hoy: aún no preguntado. Asistente: sin respuesta desde el arranque. "
        "Datos: sincronizado."
    )


def test_the_estado_cases_cover_every_phase_kind():
    assert sorted(c[0] for c in ESTADO_CASES) == sorted(cycle.KINDS)


def test_estado_is_for_admins_only(make_bot):
    bot = make_bot()
    say(bot, "/estado")
    say(bot, "/status", chat=HER, private=True)
    assert bot.tg.texts() == []
    say(bot, "/status@example_cycle_bot", user=ADMIN)
    assert bot.tg.texts()[-1].startswith("Fase: regla, día 4.")


def test_estado_tells_the_ask_the_assistant_and_unpushed_commits(make_bot):
    bot = make_bot()
    bot.kv.set("last_ask", "2027-03-04")
    say(bot, "hola", reply_to=QUESTION)  # a turn that worked
    bot.now.advance(minutes=90)
    tap(bot, "f:20270304:heavy")
    bot.repo.flush(force=True)  # committed and pushed
    bot.repo._git("commit", "--allow-empty", "--quiet", "-m", "local")  # one not pushed
    say(bot, "/estado", user=ADMIN)
    assert bot.tg.texts()[-1] == (
        "Fase: regla, día 4. Hoy: preguntado y respondido. Asistente: última respuesta hace 1 h. "
        "Datos: 1 commit(s) sin subir."
    )
    bot.repo.remote_ok = False
    bot.repo._git("reset", "--soft", "HEAD~1")
    say(bot, "/estado", user=ADMIN)
    assert bot.tg.texts()[-1].endswith("Datos: remoto inaccesible al arrancar.")


def test_estado_without_an_assistant(make_bot):
    bot = make_bot(profile=PROFILE.split("assistant:")[0])
    bot.kv.set("last_ask", "2027-03-04")
    say(bot, "/estado", user=ADMIN)
    assert "Hoy: preguntado, sin respuesta. Asistente: sin asistente." in bot.tg.texts()[-1]


def test_nuevo_clears_the_pending_lines_and_the_conversation(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    say(bot, "hola", reply_to=QUESTION)
    assert bot.kv.get(f"conv:{GROUP}") == "conv-1"
    mod.DOWN = True
    say(bot, "uno", reply_to=QUESTION)
    say(bot, "dos", reply_to=QUESTION)
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 2
    say(bot, "/nuevo", user=ADMIN)
    assert bot.kv.get_json(f"pending:{GROUP}") is None
    assert bot.kv.get(f"conv:{GROUP}") is None
    assert mod.FORGOTTEN == ["conv-1"]
    assert bot.tg.texts()[-1] == "Empiezo una conversación nueva. Lo apuntado sigue igual."
    mod.DOWN = False
    say(bot, "tres", reply_to=QUESTION)
    assert mod.CALLS[-1]["turn"]["conversation"] is None
    assert [x["content"].rsplit("] ", 1)[1] for x in mod.CALLS[-1]["turn"]["new"]] == ["tres"]


def test_nuevo_is_for_admins_and_a_failed_forget_keeps_the_conversation(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    say(bot, "hola", reply_to=QUESTION)
    say(bot, "/nuevo")  # her: not an admin command for her
    assert bot.kv.get(f"conv:{GROUP}") == "conv-1"
    mod.forget = lambda conversation, settings: (_ for _ in ()).throw(RuntimeError("busy"))
    say(bot, "/new", user=ADMIN)
    assert bot.kv.get(f"conv:{GROUP}") == "conv-1"
    assert bot.tg.texts()[-1].startswith("No he podido borrar la conversación del asistente")


def test_ayuda_and_presentacion(make_bot):
    bot = make_bot()
    say(bot, "/ayuda")
    assert bot.tg.texts()[-1].startswith("Esto es lo que entiendo:")
    assert "/estado" not in bot.tg.texts()[-1]
    say(bot, "/help", user=ADMIN)
    assert "Para administradores:\n/estado" in bot.tg.texts()[-1]
    say(bot, "/presentacion")  # her: nothing
    assert len(bot.tg.texts()) == 2
    say(bot, "/about", user=ADMIN)
    assert bot.tg.texts()[-1].startswith("Hola. Llevo la cuenta del ciclo de Lucía")
    say(bot, "/presentacion", user=STRANGER)
    assert len(bot.tg.texts()) == 3


def test_migration_moves_the_bot_to_the_new_chat(make_bot):
    bot = make_bot()
    bot.handle({"message": {"chat": {"id": GROUP}, "migrate_to_chat_id": -100456}})
    assert bot.chat_id == -100456
    say(bot, "hola", chat=GROUP, reply_to=QUESTION)
    assert plugin(bot).CALLS == []
    say(bot, "hola", chat=-100456, reply_to=QUESTION)
    assert len(plugin(bot).CALLS) == 1
    bot.handle({"message": {"chat": {"id": -100999}, "migrate_to_chat_id": -100777}})
    assert bot.chat_id == -100456  # another chat's migration is not ours


# --- the loop ------------------------------------------------------------------------------


def test_poll_handles_updates_and_keeps_the_offset(make_bot):
    bot = make_bot()
    bot.tg.updates = [
        [
            {"update_id": 41, "message": {"chat": {"id": GROUP}, "text": "x", "from": {"id": 7}}},
            {"update_id": 42, "callback_query": {"id": "c", "from": {"id": HER}, "data": "bad"}},
        ]
    ]
    assert bot.poll_once() == 0
    assert bot.kv.get("offset") == "43"
    assert bot.poll_once() == 0
    assert bot.tg.of("getUpdates")[-1]["offset"] == 43
    assert bot.tg.of("getUpdates")[-1]["allowed_updates"] == ["message", "callback_query"]


def test_poll_waits_on_409_and_429_and_backs_off_on_5xx(make_bot):
    bot = make_bot()
    bot.kv.set("offset", "7")
    bot.tg.fail_next("getUpdates", 409, "Conflict: terminated by other getUpdates request")
    assert bot.poll_once() == 30
    assert bot.kv.get("offset") == "7"
    bot.tg.fail_next("getUpdates", 429, "Too Many Requests", {"retry_after": 12})
    assert bot.poll_once() == 12
    for expected in (5, 10, 20):
        bot.tg.fail_next("getUpdates", 502, "Bad Gateway")
        assert bot.poll_once() == expected
    bot.tg.fail_next("getUpdates", 0, "URLError")
    assert bot.poll_once() == 40
    assert bot.poll_once() == 0  # a success resets the backoff
    bot.tg.fail_next("getUpdates", 502, "Bad Gateway")
    assert bot.poll_once() == 5
    bot.tg.fail_next("getUpdates", 401, "Unauthorized")
    with pytest.raises(TelegramError):
        bot.poll_once()


def test_a_bad_update_does_not_stop_the_loop(make_bot, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(bot, "on_message", lambda msg: 1 / 0)
    bot.tg.updates = [[{"update_id": 5, "message": {}}]]
    assert bot.poll_once() == 0
    assert bot.kv.get("offset") == "6"


def test_run_sets_up_once_and_flushes_at_once_on_sigterm(make_bot):
    bot = make_bot()
    flushes = []
    bot.repo.flush = lambda force=False: flushes.append(force) or "idle"
    stop = threading.Event()

    def interrupted():
        stop.set()
        raise Interrupted  # what the SIGTERM handler raises mid long poll

    bot.tg.updates = [[], interrupted]
    bot.run(stop)
    assert len(bot.tg.of("setMyCommands")) == 1
    assert len(bot.tg.of("getUpdates")) == 2
    assert flushes == [False, True]  # one tick, then the forced flush on the way out
    assert bot.polling is False


def test_run_stops_on_a_bad_token(make_bot):
    bot = make_bot()
    bot.tg.fail_next("getMe", 401, "Unauthorized")
    with pytest.raises(TelegramError):
        bot.run(threading.Event())
