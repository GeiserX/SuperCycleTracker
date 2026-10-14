"""The chat log and its recap, offers with buttons that do nothing without an answer, the calendar
plan offered with one tap, and reminders: offered, set, posted, listed and cancelled."""

import itertools
from datetime import date

from conftest import ADMIN, GROUP, HER, PROFILE, STRANGER, at
from test_bot import QUESTION, buttons, plugin, say, tap, toasts

from supercycletracker import assistant, cycle
from supercycletracker.bot import load_profile, profile_errors

_mids = itertools.count(40_000)

ALL = PROFILE.replace("  admins: {222: Marta}\n", "  admins: {222: Marta}\n  history_scope: all\n")
CAL = ALL + "calendar:\n  events: true\n  reminders: true\n"


def group_text(bot, text, user=ADMIN, first_name=None):
    msg = {
        "message_id": next(_mids),
        "from": {"id": user, **({"first_name": first_name} if first_name else {})},
        "chat": {"id": GROUP, "type": "supergroup"},
        "text": text,
    }
    bot.handle({"message": msg})
    bot.join_idle()
    return msg


def sent(bot):
    return bot.tg.of("sendMessage")


def last_turn(bot):
    return plugin(bot).CALLS[-1]["turn"]


def contents(turn):
    return [x["content"] for x in turn["new"]]


def markups(bot):
    return [p["reply_markup"] for p in bot.tg.of("editMessageReplyMarkup")]


def labels(markup):
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


# --- 1. the chat log and the recap ---------------------------------------------------------


def test_group_talk_is_context_for_the_next_turn_marked_and_never_answered(make_bot):
    bot = make_bot(profile=ALL)
    group_text(bot, "¿compro ibuprofeno?")  # Marta to the group, not to the bot
    group_text(bot, "vale", user=STRANGER, first_name="Pepe")
    assert plugin(bot).CALLS == [] and sent(bot) == []  # seen, not answered
    say(bot, "me duele un poco", reply_to=QUESTION)
    assert contents(last_turn(bot)) == [
        "[not to you] Marta: ¿compro ibuprofeno?",
        "[not to you] Pepe: vale",
        f"Lucía: [replying to the bot: {QUESTION}] me duele un poco",
    ]
    say(bot, "y cansada", reply_to=QUESTION)  # a kept turn took them: not again
    assert contents(last_turn(bot)) == [f"Lucía: [replying to the bot: {QUESTION}] y cansada"]


def test_by_default_group_talk_is_not_kept(make_bot):
    bot = make_bot()  # history_scope: asked
    group_text(bot, "¿compro ibuprofeno?")
    say(bot, "me duele un poco", reply_to=QUESTION)
    assert len(last_turn(bot)["new"]) == 1
    assert bot.kv.chat_rows(GROUP, 0) == [r for r in bot.kv.chat_rows(GROUP, 0) if r["asked"]]


def test_a_failed_turn_keeps_the_context_for_the_next(make_bot):
    bot = make_bot(profile=ALL)
    group_text(bot, "¿compro ibuprofeno?")
    plugin(bot).REPLIES[:] = [{"text": "x", "kept": False}]
    say(bot, "uno", reply_to=QUESTION)
    say(bot, "dos", reply_to=QUESTION)
    assert contents(last_turn(bot))[0] == "[not to you] Marta: ¿compro ibuprofeno?"


def test_the_recap_is_the_chat_log_without_the_new_lines(make_bot):
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [{"text": "Anotado.", "conversation": "c1", "kept": True}]
    say(bot, "ayer poco", reply_to=QUESTION)
    group_text(bot, "qué bien")
    bot.now.advance(minutes=5)
    say(bot, "hoy nada", reply_to=QUESTION)
    turn = last_turn(bot)
    assert [(h["role"], h["content"]) for h in turn["history"]] == [
        ("user", f"Lucía: [replying to the bot: {QUESTION}] ayer poco"),
        ("assistant", "Anotado."),
    ]
    assert all(h["ts"].endswith("+00:00") for h in turn["history"])
    assert contents(turn)[0] == "[not to you] Marta: qué bien"  # new, so not in the recap too
    say(bot, "y mañana", reply_to=QUESTION)
    assert ("user", "[not to you] Marta: qué bien") in [
        (h["role"], h["content"]) for h in last_turn(bot)["history"]
    ]


def test_the_recap_reaches_back_history_days_only(make_bot):
    bot = make_bot(
        profile=ALL.replace("  history_scope: all\n", "  history_scope: all\n  history_days: 2\n")
    )
    say(bot, "lo de hace tiempo", reply_to=QUESTION)
    bot.now.advance(days=3)
    say(bot, "ayer", reply_to=QUESTION)
    bot.now.advance(minutes=1)
    say(bot, "hoy", reply_to=QUESTION)
    shown = [h["content"] for h in last_turn(bot)["history"]]
    assert any(c.endswith("] ayer") for c in shown)
    assert not any("hace tiempo" in c for c in shown)


def test_history_days_zero_sends_no_recap(make_bot):
    bot = make_bot(
        profile=ALL.replace("  history_scope: all\n", "  history_scope: all\n  history_days: 0\n")
    )
    say(bot, "uno", reply_to=QUESTION)
    say(bot, "dos", reply_to=QUESTION)
    assert last_turn(bot)["history"] == []


def test_an_edit_of_group_talk_changes_the_log_and_the_next_turn_sees_it(make_bot):
    bot = make_bot(profile=ALL)
    msg = group_text(bot, "compro ibuprofeno")
    stranger = group_text(bot, "voy yo", user=STRANGER, first_name="Pepe")
    bot.handle({"edited_message": {**msg, "text": "compro paracetamol"}})
    bot.handle({"edited_message": {**stranger, "text": "voy yo luego"}})
    say(bot, "gracias", reply_to=QUESTION)
    assert contents(last_turn(bot))[:2] == [
        "[not to you] Marta: compro paracetamol",
        "[not to you] Pepe: voy yo luego",
    ]


def test_an_edit_that_adds_the_mention_turns_the_logged_line_into_a_message(make_bot):
    bot = make_bot(profile=ALL)
    msg = group_text(bot, "apunta que me duele")
    text = "@example_cycle_bot apunta que me duele"
    entity = [{"type": "mention", "offset": 0, "length": 18}]
    bot.handle({"edited_message": {**msg, "text": text, "entities": entity}})
    bot.join_idle()
    assert contents(last_turn(bot)) == ["Marta: apunta que me duele"]  # once, as a message
    rows = bot.kv.chat_rows(GROUP, 0)
    assert [(r["text"], r["asked"]) for r in rows if r["role"] == "user"] == [
        ("Marta: apunta que me duele", 1)
    ]


def test_new_conversation_forgets_the_log(make_bot):
    bot = make_bot(profile=ALL)
    group_text(bot, "algo")
    say(bot, "uno", reply_to=QUESTION)
    say(bot, "/nuevo", user=ADMIN)
    assert bot.kv.chat_rows(GROUP, 0) == []
    say(bot, "dos", reply_to=QUESTION)
    assert last_turn(bot)["history"] == []


def test_history_settings_are_checked(make_bot):
    bot = make_bot()
    good = load_profile(bot.data)
    assert (good.history_scope, good.history_days) == ("asked", 14)
    for line, code in (
        ("  history_scope: some\n", "bad_history_scope"),
        ("  history_days: 61\n", "bad_history_days"),
        ("  history_days: many\n", "bad_history_days"),
    ):
        text = PROFILE.replace("  admins: {222: Marta}\n", "  admins: {222: Marta}\n" + line)
        (bot.data / "cycle" / "profile.yaml").write_text(text, encoding="utf-8")
        assert code in profile_errors(load_profile(bot.data), bot.data)
    bad = PROFILE + "calendar:\n  titles: {moon: Luna}\n"
    (bot.data / "cycle" / "profile.yaml").write_text(bad, encoding="utf-8")
    assert "bad_calendar_titles" in profile_errors(load_profile(bot.data), bot.data)


# --- 2. offers: buttons that expire, and no answer does nothing ---------------------------


def test_an_offer_says_no_answer_does_nothing_and_expires_into_nothing(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        {"text": "¿Te pregunto a las 22:00?", "kept": True, "options": ["Sí", "No"]}
    ]
    say(bot, "me preguntas muy pronto", reply_to=QUESTION)
    offer = sent(bot)[-1]
    assert offer["text"].endswith("Si no respondes, no hago nada.")
    bot.now.advance(hours=12, minutes=1)
    bot.expire_options()
    assert markups(bot)[-1] == {
        "inline_keyboard": [[{"text": "Sin respuesta: nada", "callback_data": "x"}]]
    }
    calls = len(plugin(bot).CALLS)
    tap(bot, buttons(offer["reply_markup"])[0], message_id=offer.get("message_id", 1))
    bot.join_idle()
    assert toasts(bot)[-1] == "Esto ya no está esperando respuesta."
    assert len(plugin(bot).CALLS) == calls  # a late tap does nothing
    assert bot.kv.get_json(f"options:{GROUP}") is None


# --- 3. the rules: who reaches the bot -----------------------------------------------------


def test_the_rules_say_who_reaches_the_bot_and_that_a_remark_is_no_order():
    off = assistant.rules("Lucía", "es", ["Marta"])
    on = assistant.rules("Lucía", "es", ["Marta"], her_text=True)
    for text in (off, on):
        assert "{reach}" not in text
        assert "mentioning you, by replying to one of your messages, or with a voice note" in text
        assert '"[not to you]"' in text and "act only on what is addressed to you" in text
        assert "A remark between people about you" in text
    assert "every message Lucía writes reaches you too" in on
    assert "every message Lucía writes" not in off


def test_the_bot_hands_the_rules_her_text_rule_when_it_reads_the_group(make_bot):
    bot = make_bot(read_all=True)
    say(bot, "hola")  # plain text from her: for the bot when it reads the group
    assert "every message Lucía writes reaches you too" in last_turn(bot)["rules"]


# --- 4. the calendar plan ------------------------------------------------------------------


def seed(bot):
    from supercycletracker import records

    return records.load(bot.repo.log_path)


def test_the_plan_follows_the_prediction(make_bot):
    bot = make_bot()
    log_ = seed(bot)
    plan = cycle.calendar_plan(log_, date(2027, 3, 4), ("follicular", "luteal", "period"))
    assert [(e.kind, e.start.isoformat(), e.end.isoformat()) for e in plan] == [
        ("follicular", "2027-03-06", "2027-03-16"),  # after her usual 5 days
        ("luteal", "2027-03-16", "2027-03-29"),
        ("period", "2027-03-29", "2027-04-03"),
    ]
    assert cycle.calendar_plan(log_, date(2027, 3, 4)) == plan  # every kind by default
    later = cycle.calendar_plan(log_, date(2027, 3, 20), ("follicular", "luteal", "period"))
    assert [e.kind for e in later] == ["luteal", "period"]  # what ended is left out
    # late (the window closed on 1 April) while the predicted period still reaches today
    assert cycle.calendar_plan(log_, date(2027, 4, 2)) == ()


def test_the_plan_goes_in_context_only_when_the_profile_turns_it_on(make_bot):
    bot = make_bot()
    say(bot, "hola", reply_to=QUESTION)
    assert "# Calendar plan" not in last_turn(bot)["context"]
    bot = make_bot(profile=CAL)
    say(bot, "hola", reply_to=QUESTION)
    context = last_turn(bot)["context"]
    assert "# Calendar plan" in context
    assert (
        '{"summary": "Regla prevista", "start": {"date": "2027-03-29"}, '
        '"end": {"date": "2027-04-03"}}'
    ) in context


def offered_plan(bot):
    assert bot.offer_plan(bot.now())
    return sent(bot)[-1]


def test_the_plan_is_offered_once_in_waking_hours(make_bot):
    bot = make_bot(profile=CAL)
    bot.now.at = at(date(2027, 3, 4), 23, 30)
    assert not bot.offer_plan(bot.now())  # night: not now
    bot.now.at = at(date(2027, 3, 5), 10, 0)
    post = offered_plan(bot)
    assert post["text"] == (
        "@lucia_example, ¿pongo en tu calendario la previsión de tu ciclo?\n"
        "• Fase folicular: del 6 de marzo al 15 de marzo\n"
        "• Fase lútea: del 16 de marzo al 28 de marzo\n"
        "• Regla prevista: del 29 de marzo al 2 de abril\n\n"
        "Si no respondes, no hago nada."
    )
    assert labels(post["reply_markup"]) == ["En el calendario", "Nada"]
    assert not bot.offer_plan(bot.now())  # the same plan is not offered twice
    bot.now.advance(hours=12, minutes=1)
    bot.expire_offers()
    assert labels(markups(bot)[-1]) == ["✓ Sin respuesta: nada"]
    assert not bot.offer_plan(bot.now())  # no answer did nothing, and it is not offered again


def test_the_plan_is_off_without_the_profile_switch_and_titles_are_configurable(make_bot):
    bot = make_bot(profile=ALL)
    assert not bot.offer_plan(bot.now())
    titles = "  titles: {luteal: Días antes, follicular: ''}\n"
    bot = make_bot(profile=CAL + titles)
    post = offered_plan(bot)
    assert "• Días antes: del 16 de marzo al 28 de marzo" in post["text"]
    assert "folicular" not in post["text"]


def plan_ask(title, start, end, n=1):
    return {
        "id": f"job:{n}",
        "summary": f"Quiero añadir esto a tu calendario: {title}",
        "confirm_by": "asker",
        "expires_at": 4_000_000_000,
        "action": "add",
        "event": {"title": title, "start": start, "end": end},
    }


def test_one_tap_on_the_plan_approves_exactly_its_entries(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_plan(bot)
    mod = plugin(bot)
    asks = [
        plan_ask("Fase folicular", "2027-03-06", "2027-03-16", 1),
        plan_ask("Fase lútea", "2027-03-16", "2027-03-29", 2),
        plan_ask("Regla prevista", "2027-03-29", "2027-04-03", 3),
        plan_ask("Regla prevista", "2027-03-29", "2027-04-03", 4),  # the same entry again
        plan_ask("Regla prevista", "2027-03-30", "2027-04-03", 5),  # not in the plan
    ]
    got = []

    def reply(turn):
        got.extend(turn["ask"](a) for a in asks)
        return {"text": "Hecho, ya lo tienes.", "conversation": "c", "kept": True}

    mod.REPLIES[:] = [reply]
    tap(bot, buttons(post["reply_markup"])[0], message_id=post.get("message_id", 1))
    bot.join_idle()
    new = contents(last_turn(bot))
    assert new[0].startswith("Lucía: [pedido con el botón «En el calendario» de la previsión]")
    assert '{"title": "Regla prevista", "start": "2027-03-29", "end": "2027-04-03"}' in new[0]
    approved = [r["id"] for r, d in mod.ANSWERED if d["approved"]]
    assert approved == ["job:1", "job:2", "job:3"]
    assert all(d["by"] == str(HER) for _, d in mod.ANSWERED)
    texts = [p["text"] for p in sent(bot)]
    assert (
        "Aprobado con el botón «En el calendario»: Fase lútea: del 16 de marzo al 28 de marzo\nHecho."
        in texts
    )
    asked = [t for t in texts if "¿lo apruebas?" in t]
    assert len(asked) == 2  # the repeat and the entry outside the plan are asked as usual
    assert got == [True] * 5


def test_a_plan_tap_after_the_plan_moved_adds_nothing_and_offers_again(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_plan(bot)
    renamed = CAL + "  titles: {luteal: Días antes}\n"
    (bot.data / "cycle" / "profile.yaml").write_text(renamed, encoding="utf-8")
    assert bot.reload_profile()  # the plan's key changed under the offer
    calls = len(plugin(bot).CALLS)
    tap(bot, buttons(post["reply_markup"])[0], message_id=post.get("message_id", 1))
    bot.join_idle()
    assert toasts(bot)[-1] == "La previsión ha cambiado desde entonces; te ofrezco la nueva."
    assert len(plugin(bot).CALLS) == calls
    assert labels(markups(bot)[-1]) == ["✓ Sin respuesta: nada"]
    again = offered_plan(bot)
    assert "• Días antes: del 16 de marzo al 28 de marzo" in again["text"]


def test_nothing_under_the_plan_does_nothing_and_strangers_cannot_tap(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_plan(bot)
    yes, no = buttons(post["reply_markup"])
    tap(bot, yes, user=STRANGER, message_id=post.get("message_id", 1))
    assert toasts(bot)[-1] == "Esto es solo para Lucía."
    tap(bot, no, message_id=post.get("message_id", 1))
    bot.join_idle()
    assert plugin(bot).CALLS == []
    assert labels(markups(bot)[-1]) == ["✓ Nada"]


# --- 5. reminders --------------------------------------------------------------------------


REMINDERS = (
    "Tómate algo para el dolor antes de dormir.\n\n"
    "```reminders\n- 2027-03-04 23:00 | Tómate el ibuprofeno\n"
    "- 2027-03-01 09:00 | Esto ya pasó\n```"
)


def offered_reminder(bot, text=REMINDERS):
    plugin(bot).REPLIES[:] = [{"text": text, "conversation": "c", "kept": True}]
    say(bot, "me duele mucho", reply_to=QUESTION)
    return sent(bot)[-1]


def test_reminders_in_an_answer_are_offered_with_buttons_and_never_shown_as_a_block(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    answer = sent(bot)[-2]
    assert answer["text"] == "Tómate algo para el dolor antes de dormir."
    assert post["text"] == (
        "@lucia_example, ¿quieres que te lo recuerde?\n"
        "jueves 4 de marzo, 23:00: Tómate el ibuprofeno\n\n"  # the past one is dropped
        "Si no respondes, no hago nada."
    )
    assert labels(post["reply_markup"]) == ["Recordatorio aquí", "En el calendario", "Nada"]
    bot = make_bot(profile=ALL)  # without calendar.reminders: no calendar button
    assert labels(offered_reminder(bot)["reply_markup"]) == ["Recordatorio aquí", "Nada"]


def test_a_reminder_set_here_is_posted_at_its_time_once(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    tap(bot, buttons(post["reply_markup"])[0], message_id=post.get("message_id", 1))
    assert toasts(bot)[-1] == "Te lo recuerdo aquí el jueves 4 de marzo, 23:00."
    assert labels(markups(bot)[-1]) == ["✓ Recordatorio aquí"]
    bot.tick_reminders()
    assert "⏰" not in sent(bot)[-1]["text"]  # not yet
    bot.now.at = at(date(2027, 3, 4), 23, 0)
    bot.tick_reminders()
    assert sent(bot)[-1]["text"] == "⏰ @lucia_example: Tómate el ibuprofeno"
    bot.tick_reminders()
    assert sum("⏰" in p["text"] for p in sent(bot)) == 1


def test_a_reminder_after_a_restart_says_it_is_late(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    tap(bot, buttons(post["reply_markup"])[0], message_id=post.get("message_id", 1))
    bot.now.at = at(date(2027, 3, 5), 8, 0)
    bot.tick_reminders()
    assert sent(bot)[-1]["text"] == (
        "⏰ @lucia_example: Tómate el ibuprofeno\n"
        "(con retraso: era para el jueves 4 de marzo, 23:00)"
    )


def test_reminders_are_listed_and_cancelled(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    tap(bot, buttons(post["reply_markup"])[0], message_id=post.get("message_id", 1))
    say(bot, "/recordatorios")
    listing = sent(bot)[-1]
    assert listing["text"] == (
        "⏰ Recordatorios pendientes:\n1. jueves 4 de marzo, 23:00 · Lucía: Tómate el ibuprofeno"
    )
    (cancel,) = buttons(listing["reply_markup"])
    bot.handle(
        {
            "callback_query": {
                "id": "cb-k",
                "from": {"id": HER},
                "message": {
                    "message_id": 77,
                    "chat": {"id": GROUP, "type": "supergroup"},
                    "reply_markup": listing["reply_markup"],
                },
                "data": cancel,
            }
        }
    )
    assert toasts(bot)[-1] == "Cancelado."
    assert labels(markups(bot)[-1]) == ["✓ Cancelado 1"]
    bot.now.at = at(date(2027, 3, 4), 23, 0)
    bot.tick_reminders()
    assert not any("⏰ @" in p["text"] for p in sent(bot))
    say(bot, "/recordatorios")
    assert sent(bot)[-1]["text"] == "No hay recordatorios pendientes en este chat."


def test_a_reminder_offer_nobody_answers_does_nothing(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    bot.now.advance(minutes=31)
    bot.expire_offers()
    assert labels(markups(bot)[-1]) == ["✓ Sin respuesta: nada"]
    tap(bot, buttons(post["reply_markup"])[0], message_id=post.get("message_id", 1))
    assert toasts(bot)[-1] == "Esto ya no está esperando respuesta."
    assert bot.kv.reminders() == []


def reminder_ask(when, n=1):
    return {
        "id": f"job:{n}",
        "summary": "Quiero añadir esto a tu calendario: Tómate el ibuprofeno",
        "confirm_by": "asker",
        "expires_at": 4_000_000_000,
        "action": "add",
        "when": when,
    }


def test_the_calendar_button_of_a_reminder_approves_that_one_addition(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    mod = plugin(bot)
    at_ = at(date(2027, 3, 4), 23, 0).timestamp()

    def reply(turn):
        turn["ask"](reminder_ask(at_, 1))
        turn["ask"](reminder_ask(at_, 2))  # a second addition is asked as usual
        return {"text": "Lo tienes en el calendario.", "conversation": "c", "kept": True}

    mod.REPLIES[:] = [reply]
    tap(bot, buttons(post["reply_markup"])[1], message_id=post.get("message_id", 1))
    bot.join_idle()
    assert contents(last_turn(bot))[-1].startswith(
        "Lucía: [pedido con el botón «En el calendario» de un recordatorio] Añade a mi calendario"
    )
    assert [(r["id"], d["approved"]) for r, d in mod.ANSWERED] == [("job:1", True)]
    assert sum("¿lo apruebas?" in p["text"] for p in sent(bot)) == 1


def test_a_reminder_grant_does_not_approve_another_time(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    mod = plugin(bot)
    other = at(date(2027, 3, 4), 22, 0).timestamp()
    mod.REPLIES[:] = [
        lambda turn: (turn["ask"](reminder_ask(other)), {"text": "x", "kept": True})[1]
    ]
    tap(bot, buttons(post["reply_markup"])[1], message_id=post.get("message_id", 1))
    bot.join_idle()
    assert mod.ANSWERED == []  # asked with Aprobar, not approved by the tap
    assert sum("¿lo apruebas?" in p["text"] for p in sent(bot)) == 1


def test_only_the_person_answered_may_tap_a_reminder(make_bot):
    bot = make_bot(profile=CAL)
    post = offered_reminder(bot)
    tap(bot, buttons(post["reply_markup"])[0], user=ADMIN, message_id=post.get("message_id", 1))
    assert toasts(bot)[-1] == "Esto solo lo puede responder Lucía."
    assert bot.kv.reminders() == []


def test_reminder_blocks_are_parsed_strictly():
    text, got = assistant.extract_reminders(
        "Hola.\n\n```reminders\n- 2027-03-04 23:00 | Uno\n- mañana | Dos\n- 2027-03-05 9:00 | Tres\n"
        "- 2027-03-04 23:00 | Uno\n```"
    )
    assert text == "Hola."
    assert got == ({"when": "2027-03-04 23:00", "text": "Uno"},)
    assert assistant.reminders([{"when": "2027-03-04T23:00", "text": "x" * 301}]) == ()
