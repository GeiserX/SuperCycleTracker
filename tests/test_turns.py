"""One answer per burst, messages handed to a running turn, edits, choices as buttons, and the
plugin's asks (an action the assistant wants to take, approved by a person)."""

import itertools
import threading
import time

from conftest import (
    ADMIN,
    BOT_ID,
    GROUP,
    HER,
    PROFILE,
    STRANGER,
)
from test_bot import QUESTION, buttons, plugin, say, toasts

_mids = itertools.count(20_000)


def message(text, user=HER, mid=None, reply=True, entities=None):
    msg = {
        "message_id": mid if mid is not None else next(_mids),
        "from": {"id": user},
        "chat": {"id": GROUP, "type": "supergroup"},
        "text": text,
    }
    if reply:
        msg["reply_to_message"] = {"message_id": 1, "from": {"id": BOT_ID}, "text": QUESTION}
    if entities:
        msg["entities"] = entities
    return msg


def edit(msg, text, **extra):
    return {"edited_message": {**msg, "text": text, **extra}}


def tap(bot, data, user=HER, message_id=1):
    bot.handle(
        {
            "callback_query": {
                "id": f"cb-{data}-{user}",
                "from": {"id": user},
                "message": {"message_id": message_id, "chat": {"id": GROUP, "type": "supergroup"}},
                "data": data,
            }
        }
    )


def sent(bot):
    return bot.tg.of("sendMessage")


def answer_of(bot, text):
    return next(p for p in sent(bot) if p["text"] == text)


def fake_time(bot, at=None):
    """The bot's debounce clock moved only by its own sleeps; `at[t]` runs once the clock has
    reached `t`, on the worker thread, as a message that arrives meanwhile."""
    now = [0.0]
    at = dict(at or {})
    bot.clock = lambda: now[0]

    def sleep(seconds):
        for t in sorted(at):
            if now[0] >= t:
                at.pop(t)()
        now[0] += seconds

    bot.sleep = sleep
    return now


def contents(call):
    return [x["content"] for x in call["turn"]["new"]]


# --- one answer per burst ------------------------------------------------------------------


def test_a_burst_goes_as_one_turn_and_gets_one_plain_answer(make_bot):
    bot = make_bot()
    bot.debounce_s = 2.5
    second = message("y me duele")
    fake_time(bot, {1.0: lambda: bot.handle({"message": second})})
    bot.handle({"message": message("me ha bajado")})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 1
    assert [c.rsplit("] ", 1)[1] for c in contents(calls[0])] == ["me ha bajado", "y me duele"]
    assert "reply_parameters" not in answer_of(bot, "Vale.")  # one answer for two: no quote
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_one_message_gets_an_answer_that_replies_to_it(make_bot):
    bot = make_bot()
    msg = say(bot, "hoy poco", reply_to=QUESTION)
    assert answer_of(bot, "Vale.")["reply_parameters"]["message_id"] == msg["message_id"]


def test_a_voice_note_still_being_heard_holds_the_turn(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    gate = threading.Event()
    mod.VOICES[:] = [lambda: "me duele la cabeza" if gate.wait(5) else ""]
    bot.tg.files["v1"] = b"OggS"
    note = {
        "message_id": next(_mids),
        "from": {"id": HER},
        "chat": {"id": GROUP, "type": "supergroup"},
        "voice": {"file_id": "v1", "duration": 3},
    }
    bot.handle({"message": note})
    bot.handle({"message": message("y estoy cansada")})
    time.sleep(0.4)
    assert mod.CALLS == []  # the text waits for the note
    gate.set()
    bot.join_idle()
    assert len(mod.CALLS) == 1
    assert contents(mod.CALLS[0]) == [
        "Lucía: [voice note] me duele la cabeza",
        f"Lucía: [replying to the bot: {QUESTION}] y estoy cansada",
    ]


def test_a_note_heard_too_late_goes_with_the_next_turn(make_bot):
    bot = make_bot()
    bot.voice_wait_s = 0.2
    mod = plugin(bot)
    gate = threading.Event()
    mod.VOICES[:] = [lambda: "me duele" if gate.wait(5) else ""]
    bot.tg.files["v1"] = b"OggS"
    note = {
        "message_id": next(_mids),
        "from": {"id": HER},
        "chat": {"id": GROUP, "type": "supergroup"},
        "voice": {"file_id": "v1", "duration": 3},
    }
    bot.handle({"message": note})
    bot.handle({"message": message("y estoy cansada")})
    deadline = time.monotonic() + 5
    while not mod.CALLS and time.monotonic() < deadline:
        time.sleep(0.05)
    gate.set()
    bot.join_idle()
    assert [len(c["turn"]["new"]) for c in mod.CALLS] == [1, 1]
    assert contents(mod.CALLS[1]) == ["Lucía: [voice note] me duele"]


# --- messages handed to the running turn ---------------------------------------------------


def test_a_message_handed_to_the_running_turn_gets_the_same_answer(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    late = message("y ahora más")

    def handover(turn):
        bot.handle({"message": late})
        got = turn["incoming"]()
        assert [g["content"].rsplit("] ", 1)[1] for g in got] == ["y ahora más"]
        assert turn["incoming"]() == []  # handed over once
        return {"text": "Las dos.", "conversation": "c", "kept": True, "covers": [got[0]["id"]]}

    mod.REPLIES[:] = [handover]
    say(bot, "hoy poco", reply_to=QUESTION)
    assert len(mod.CALLS) == 1
    assert bot.tg.texts() == ["Las dos."]
    assert "reply_parameters" not in answer_of(bot, "Las dos.")
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_handed_over_message_the_answer_did_not_cover_goes_with_the_next_turn(make_bot):
    bot = make_bot()
    mod = plugin(bot)

    def handover(turn):
        bot.handle({"message": message("otra cosa")})
        assert len(turn["incoming"]()) == 1
        return {"text": "Primera.", "conversation": "c", "kept": True, "covers": []}

    mod.REPLIES[:] = [handover]
    say(bot, "hoy poco", reply_to=QUESTION)
    assert len(mod.CALLS) == 2
    assert [c.rsplit("] ", 1)[1] for c in contents(mod.CALLS[1])] == ["otra cosa"]
    assert bot.tg.texts() == ["Primera.", "Vale."]


def test_a_late_call_to_incoming_after_the_turn_takes_nothing(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    kept = {}
    mod.REPLIES[:] = [lambda turn: kept.update(incoming=turn["incoming"]) or "Vale."]
    say(bot, "hoy poco", reply_to=QUESTION)
    mod.DOWN = True  # the next turn fails, so its line stays queued
    say(bot, "después", reply_to=QUESTION)
    assert kept["incoming"]() == []
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 1


# --- edits ---------------------------------------------------------------------------------


def test_an_edit_of_a_queued_message_sends_the_new_wording(make_bot):
    bot = make_bot()
    bot.debounce_s = 2.5
    first = message("hoy poco")
    fake_time(bot, {1.0: lambda: bot.handle(edit(first, "hoy mucho"))})
    bot.handle({"message": first})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 1
    assert [c.rsplit("] ", 1)[1] for c in contents(calls[0])] == ["hoy mucho"]


def test_an_edit_of_an_answered_message_asks_to_check_the_answer(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    mod.REPLIES[:] = ["Apuntado poco."]
    msg = say(bot, "hoy poco", reply_to=QUESTION)
    bot.handle(edit(msg, "hoy mucho"))
    bot.join_idle()
    assert len(mod.CALLS) == 2
    assert contents(mod.CALLS[1]) == [
        (
            "[Lucía edited a message you already answered. Check whether your answer still holds "
            "with the new wording, and say what changes.]\nBefore: hoy poco\nNow: hoy mucho\n"
            "Your answer was: Apuntado poco."
        )
    ]
    assert answer_of(bot, "Vale.")["reply_parameters"]["message_id"] == msg["message_id"]


def test_an_edit_that_changes_no_word_or_is_a_command_does_nothing(make_bot):
    bot = make_bot()
    msg = say(bot, "hoy poco", reply_to=QUESTION)
    bot.handle(edit(msg, "hoy  poco "))
    bot.handle(edit(msg, "/hoy"))
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1


def test_an_edit_that_adds_the_mention_is_a_new_message(make_bot):
    bot = make_bot()
    plain = message("me duele la tripa", reply=False)
    bot.handle({"message": plain})  # privacy on: not for the bot
    bot.join_idle()
    assert plugin(bot).CALLS == []
    text = "@example_cycle_bot me duele la tripa"
    bot.handle(edit(plain, text, entities=[{"type": "mention", "offset": 0, "length": 18}]))
    bot.join_idle()
    assert [c.rsplit(": ", 1)[1] for c in contents(plugin(bot).CALLS[0])] == ["me duele la tripa"]


def test_an_edit_during_the_turn_declines_its_asks_and_joins_as_a_correction(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    first = message("pon una cita el lunes")
    seen = {}

    def turn_with_an_edit(turn):
        turn["ask"](ask_request("job1:ap1"))
        bot.handle(edit(first, "pon una cita el martes"))
        bot.side.join_idle()
        seen["got"] = turn["incoming"]()
        return {"text": "Cambiado al martes.", "kept": True, "covers": [seen["got"][0]["id"]]}

    mod.REPLIES[:] = [turn_with_an_edit]
    bot.handle({"message": first})
    bot.join_idle()
    assert [g["content"] for g in seen["got"]] == [
        (
            "[Lucía edited a message of this turn. Reconsider your answer with the new wording.]\n"
            "Before: pon una cita el lunes\nNow: pon una cita el martes"
        )
    ]
    assert mod.ANSWERED == [
        (
            ask_request("job1:ap1"),
            {"approved": False, "by": "bot", "note": "editado: pon una cita el martes"},
        )
    ]
    assert "Has editado tu mensaje, así que no he hecho este cambio." in "".join(bot.tg.texts())
    assert len(mod.CALLS) == 1


# --- asks ----------------------------------------------------------------------------------


def ask_request(rid="job1:ap1", confirm_by="asker", expires_in=60):
    return {
        "id": rid,
        "summary": 'Quiero añadir esto al calendario:\n{"summary": "Cita <revisión>"}',
        "confirm_by": confirm_by,
        "expires_at": round(time.time() + expires_in),
    }


def asked(bot):
    """The ask messages the bot posted: (message id, callback data of its buttons)."""
    out = []
    for p in sent(bot):
        markup = p.get("reply_markup") or {}
        data = buttons(markup) if markup.get("inline_keyboard") else []
        if data and data[0].startswith("a:"):
            out.append((p, data))
    return out


def run_turn_with(bot, during, request=None, text="Hecho lo que pediste.", user=HER):
    """A turn whose plugin asks `request` and then runs `during(bot, ask message, data)`."""
    mod = plugin(bot)
    result = {}

    def turn(t):
        result["ok"] = t["ask"](request or ask_request())
        posted = asked(bot)
        if posted and during:
            during(bot, *posted[-1])
            bot.side.join_idle()
        return {"text": text, "kept": True}

    mod.REPLIES[:] = [turn]
    say(bot, "apúntame la revisión", user=user, reply_to=QUESTION)
    return result


def test_an_ask_is_posted_with_approve_and_reject_and_her_tap_approves_it(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    result = run_turn_with(bot, lambda b, p, data: tap(b, data[0]))
    assert result["ok"] is True
    ((post, _),) = asked(bot)
    assert post["text"].startswith("@lucia_example, ¿lo apruebas?\nQuiero añadir esto")
    assert "Cita &lt;revisión&gt;" in post["text"]  # the summary is shown whole, as text
    assert post["parse_mode"] == "HTML"
    assert [b["text"] for b in post["reply_markup"]["inline_keyboard"][0]] == [
        "Aprobar",
        "Rechazar",
    ]
    assert mod.ANSWERED == [(ask_request(), {"approved": True, "by": str(HER), "note": None})]
    chosen = {"inline_keyboard": [[{"text": "✓ Aprobar", "callback_data": "x"}]]}
    marked = [p for p in bot.tg.of("editMessageReplyMarkup") if p["reply_markup"] == chosen]
    assert len(marked) == 1  # the ask's buttons give way to what was chosen
    done = answer_of(bot, "Hecho.")
    assert done["reply_parameters"]["message_id"] == marked[0]["message_id"]  # under the ask


def test_a_rejection_answers_the_plugin_and_says_nothing_more(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    run_turn_with(bot, lambda b, p, data: tap(b, data[1]))
    assert mod.ANSWERED == [(ask_request(), {"approved": False, "by": str(HER), "note": None})]
    assert "Hecho." not in bot.tg.texts()


def test_a_failed_approval_says_why(make_bot):
    bot = make_bot()
    plugin(bot).ANSWERS[:] = [{"ok": False, "error": "el calendario lo ha rechazado"}]
    run_turn_with(bot, lambda b, p, data: tap(b, data[0]))
    assert "Falló: el calendario lo ha rechazado" in bot.tg.texts()


def test_only_the_asker_may_answer_and_only_once(make_bot):
    bot = make_bot()
    mod = plugin(bot)

    def taps(b, p, data):
        tap(b, data[0], user=ADMIN)
        tap(b, data[0], user=STRANGER)
        tap(b, data[0])
        b.side.join_idle()
        tap(b, data[0])

    run_turn_with(bot, taps)
    assert len(mod.ANSWERED) == 1
    assert toasts(bot)[:2] == ["Esto solo lo puede responder Lucía.", "Esto es solo para Lucía."]
    assert toasts(bot)[-1] == "Esto ya no está esperando respuesta."


def test_an_ask_for_the_admins_goes_to_the_admins(make_bot):
    bot = make_bot()
    mod = plugin(bot)

    def taps(b, p, data):
        assert p["text"].startswith("Marta, ¿lo apruebas?")
        tap(b, data[0])
        tap(b, data[0], user=ADMIN)

    run_turn_with(bot, taps, ask_request(confirm_by="admins"))
    assert toasts(bot)[0] == "Esto solo lo puede responder Marta."
    assert mod.ANSWERED == [
        (ask_request(confirm_by="admins"), {"approved": True, "by": str(ADMIN), "note": None})
    ]


def test_a_turn_with_two_peoples_messages_asks_the_admins(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    seen = {}

    def turn(t):
        bot.handle({"message": message("y yo también", user=ADMIN)})
        t["incoming"]()
        t["ask"](ask_request())
        seen["text"] = asked(bot)[-1][0]["text"]
        return "Vale."

    mod.REPLIES[:] = [turn]
    say(bot, "apúntame la revisión", reply_to=QUESTION)
    assert seen["text"].startswith("Marta, ¿lo apruebas?")


def test_an_ask_nobody_can_answer_is_declined_at_once(make_bot):
    bot = make_bot(profile=PROFILE.replace("admins: {222: Marta}", "admins: {}"))
    mod = plugin(bot)
    result = run_turn_with(bot, None, ask_request(confirm_by="admins"))
    assert result["ok"] is True
    assert asked(bot) == []
    assert mod.ANSWERED == [
        (
            ask_request(confirm_by="admins"),
            {"approved": False, "by": "bot", "note": "no one could be asked"},
        )
    ]


def test_an_ask_telegram_refuses_is_declined_and_one_it_cannot_reach_is_kept(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    seen = {}

    def turn(t):
        bot.tg.fail_next("sendMessage", 400, "Bad Request: message is too long")
        seen["refused"] = t["ask"](ask_request("job1:ap1"))
        bot.tg.fail_next("sendMessage", 0, "URLError")
        seen["unreached"] = t["ask"](ask_request("job1:ap2"))
        seen["again"] = t["ask"](ask_request("job1:ap2"))
        return "Vale."

    mod.REPLIES[:] = [turn]
    say(bot, "apúntame la revisión", reply_to=QUESTION)
    assert (seen["refused"], seen["unreached"], seen["again"]) == (True, False, True)
    assert [a[0]["id"] for a in mod.ANSWERED] == ["job1:ap1"]
    assert mod.ANSWERED[0][1]["note"] == "no one could be asked"
    assert len(asked(bot)) == 3  # three tries reached the fake; only the last one was posted
    assert len(bot.tg.of("editMessageReplyMarkup")) == 1  # its buttons go at the turn's end


def test_the_end_of_the_turn_and_the_expiry_take_the_buttons_off(make_bot):
    bot = make_bot()
    run_turn_with(bot, None)
    ((_, data),) = asked(bot)
    assert bot.asks == {}
    assert bot.tg.of("editMessageReplyMarkup")[-1]["reply_markup"] == {"inline_keyboard": []}
    tap(bot, data[0])
    assert toasts(bot)[-1] == "Esto ya no está esperando respuesta."
    assert plugin(bot).ANSWERED == []


def test_an_ask_past_its_end_cannot_be_approved(make_bot):
    bot = make_bot()

    def late(b, p, data):
        for item in b.asks.values():
            item["expires"] = time.time() - 1
        tap(b, data[0])

    run_turn_with(bot, late)
    assert toasts(bot)[-1] == "Esto ya no está esperando respuesta."
    assert plugin(bot).ANSWERED == []


def test_expire_asks_takes_the_buttons_off_an_ask_past_its_end(make_bot):
    bot = make_bot()

    def expire(b, p, data):
        for item in b.asks.values():
            item["expires"] = time.time() - 1
        b.expire_asks()
        assert b.asks == {}

    run_turn_with(bot, expire)
    assert bot.tg.of("editMessageReplyMarkup")[0]["reply_markup"] == {"inline_keyboard": []}


# --- choices under an answer ---------------------------------------------------------------


def test_choices_come_as_buttons_and_a_tap_is_her_next_message(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    mod.REPLIES[:] = [
        "¿Cuánto has sangrado hoy?\n\n```options\n- Poco\n- Normal\n- Mucho\n```",
        "Apuntado normal.",
    ]
    say(bot, "me ha bajado", reply_to=QUESTION)
    post = sent(bot)[-1]
    assert post["text"] == "¿Cuánto has sangrado hoy?\n\nO cuéntame otra cosa."
    data = buttons(post["reply_markup"])
    assert [b[0]["text"] for b in post["reply_markup"]["inline_keyboard"]] == [
        "Poco",
        "Normal",
        "Mucho",
    ]
    tap(bot, data[1], user=ADMIN)  # the choice is hers
    assert toasts(bot)[-1] == "Esto solo lo puede responder Lucía."
    question = 1001 + len(sent(bot)) - 1  # FakeTelegram's ids count up from 1001
    tap(bot, data[1], message_id=question)
    bot.join_idle()
    assert contents(mod.CALLS[1]) == ["Lucía: Normal"]
    assert answer_of(bot, "Apuntado normal.")["reply_parameters"]["message_id"] == question
    marks = [p["reply_markup"] for p in bot.tg.of("editMessageReplyMarkup")]
    assert {"inline_keyboard": [[{"text": "✓ Normal", "callback_data": "x"}]]} in marks
    tap(bot, data[0], message_id=question)
    assert toasts(bot)[-1] == "Esto ya no está esperando respuesta."


def test_a_yes_or_no_question_says_she_may_answer_otherwise(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        {"text": "¿Lo anoto como un día normal?", "kept": True, "options": ["Sí", "No"]}
    ]
    say(bot, "hoy normal", reply_to=QUESTION)
    post = sent(bot)[-1]
    assert post["text"].endswith("Toca Sí o No. Si quieres otra cosa, responde a este mensaje.")
    assert "force_reply" not in post["reply_markup"]
