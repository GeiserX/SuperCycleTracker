"""`telegram.answer_scope: all`: in the group, a text from her or an admin that does not mention the
bot starts a turn like an addressed one, marked «[no mention]», and the answer may be a reaction or
nothing at all (NO_REPLY)."""

import itertools

import pytest
from conftest import ADMIN, BOT_ID, GROUP, HER, PROFILE, STRANGER
from test_bot import QUESTION, plugin, say
from test_turns import fake_time

from supercycletracker import assistant
from supercycletracker.bot import load_profile, profile_errors

_mids = itertools.count(60_000)

ALL = PROFILE.replace("  admins: {222: Marta}\n", "  admins: {222: Marta}\n  answer_scope: all\n")
REACT = "```telegram\nreact: 👍\n```"


def message(text, user=ADMIN, first_name=None, is_bot=False):
    frm = {"id": user, **({"first_name": first_name} if first_name else {})}
    if is_bot:
        frm["is_bot"] = True
    return {
        "message_id": next(_mids),
        "from": frm,
        "chat": {"id": GROUP, "type": "supergroup"},
        "text": text,
    }


def group_text(bot, text, **kw):
    msg = message(text, **kw)
    bot.handle({"message": msg})
    bot.join_idle()
    return msg


def contents(call):
    return [x["content"] for x in call["turn"]["new"]]


def sent(bot):
    return bot.tg.of("sendMessage")


def reactions(bot):
    return [(r["message_id"], r["reaction"][0]["emoji"]) for r in bot.tg.of("setMessageReaction")]


def addressed(text, user=HER):
    """A reply to the bot's question: a message that called it."""
    msg = message(text, user=user)
    msg["reply_to_message"] = {"message_id": 1, "from": {"id": BOT_ID}, "text": QUESTION}
    return msg


def burst(bot, first, *rest, reply=None, during=None):
    """`first`, then each of `rest` a second apart within the debounce: one turn. `during`
    arrives while that turn runs and is handed over (and covered) when `reply` is a str."""
    bot.debounce_s = 2.5
    fake_time(bot, {1.0 + n: (lambda m=m: bot.handle({"message": m})) for n, m in enumerate(rest)})
    mod = plugin(bot)
    if during is not None:

        def handover(turn):
            bot.handle({"message": during})
            got = turn["incoming"]()
            return {"text": reply, "conversation": "c", "kept": True, "covers": [got[0]["id"]]}

        mod.REPLIES[:] = [handover]
    elif reply is not None:
        mod.REPLIES[:] = [reply]
    bot.handle({"message": first})
    bot.join_idle()
    return mod.CALLS


def test_an_unmentioned_line_starts_a_turn_marked_no_mention(make_bot):
    # MUT: drop the `unmentioned` fallback in on_message → no call
    bot = make_bot(profile=ALL)
    msg = group_text(bot, "quedan 10 raciones de lasaña")
    calls = plugin(bot).CALLS
    assert len(calls) == 1
    assert contents(calls[0]) == ["[no mention] Marta: quedan 10 raciones de lasaña"]
    assert calls[0]["turn"]["chat"] == {"id": GROUP, "user": ADMIN}
    rules = calls[0]["turn"]["rules"]
    assert '"[no mention]"' in rules and "NO_REPLY" in rules
    # one message: the answer replies to it, as it does to an addressed one
    assert sent(bot)[-1]["reply_parameters"]["message_id"] == msg["message_id"]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_her_unmentioned_line_and_a_reply_to_someone_else_are_marked(make_bot):
    bot = make_bot(profile=ALL)
    other = message("me duele", user=HER)
    other["reply_to_message"] = {"message_id": 7, "from": {"id": ADMIN}, "text": "x", "date": 0}
    bot.handle({"message": other})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1]) == [
        "[no mention] Lucía (replying to Marta's text): me duele"
    ]


def test_a_burst_of_unmentioned_lines_is_one_turn_and_a_reaction_goes_on_each(make_bot):
    # MUT: pass `everyone=None` to act in _converse → the reaction goes on the last only
    bot = make_bot(profile=ALL)
    bot.debounce_s = 2.5
    second = message("y he sacado 100 g de merluza", user=HER)
    fake_time(bot, {1.0: lambda: bot.handle({"message": second})})
    plugin(bot).REPLIES[:] = [REACT]
    first = message("quedan 10 raciones de lasaña")
    bot.handle({"message": first})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 1
    assert contents(calls[0]) == [
        "[no mention] Marta: quedan 10 raciones de lasaña",
        "[no mention] Lucía: y he sacado 100 g de merluza",
    ]
    assert sent(bot) == []  # a reaction only: no text
    ids = [first["message_id"], second["message_id"]]
    assert reactions(bot) == [(m, "👀") for m in ids] + [(m, "👍") for m in ids]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_an_unmentioned_line_joins_the_running_turn(make_bot):
    # MUT: drop the `unmentioned` fallback in on_message → incoming() hands nothing over
    bot = make_bot(profile=ALL)
    mod = plugin(bot)
    late = message("y quedan dos yogures")

    def handover(turn):
        bot.handle({"message": late})
        got = turn["incoming"]()
        assert [g["content"] for g in got] == ["[no mention] Marta: y quedan dos yogures"]
        return {
            "text": "Anotado todo.",
            "conversation": "c",
            "kept": True,
            "covers": [got[0]["id"]],
        }

    mod.REPLIES[:] = [handover]
    group_text(bot, "quedan 10 raciones")
    assert len(mod.CALLS) == 1
    assert bot.tg.texts() == ["Anotado todo."]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_bots_and_strangers_never_start_a_turn_and_stay_context_marked_not_to_you(make_bot):
    # MUT: drop the is_bot/bot_id check in `unmentioned` → the admin-listed bot starts a turn
    profile = ALL.replace("admins: {222: Marta}", "admins: {222: Marta, 444: Robot}")
    bot = make_bot(profile=profile)
    group_text(bot, "lista actualizada", user=444, is_bot=True)  # a bot, even one listed as admin
    group_text(bot, "eco", user=BOT_ID, first_name="Ciclo", is_bot=True)  # the bot itself
    group_text(bot, "vale", user=STRANGER, first_name="Pepe")  # someone it never answers
    assert plugin(bot).CALLS == [] and sent(bot) == []
    say(bot, "hoy poco", reply_to=QUESTION)
    assert contents(plugin(bot).CALLS[-1]) == [
        "[not to you] Robot: lista actualizada",
        "[not to you] Ciclo: eco",
        "[not to you] Pepe: vale",
        f"Lucía: [replying to the bot: {QUESTION}] hoy poco",
    ]


def test_no_reply_sends_no_text_marks_read_and_counts_as_answered(make_bot):
    # MUT: drop `drop_no_reply` in _converse → "NO_REPLY" is sent to the chat
    bot = make_bot(profile=ALL)
    mod = plugin(bot)
    mod.REPLIES[:] = [{"text": "NO_REPLY", "conversation": "c1", "kept": True}]
    msg = group_text(bot, "¿vienes a cenar, Lucía?")
    assert len(mod.CALLS) == 1
    assert sent(bot) == []  # no text, no «ai_down», no «try again»
    assert reactions(bot) == [(msg["message_id"], "👀")]  # read
    assert bot.kv.get_json(f"pending:{GROUP}") is None  # handled: not sent again
    assert bot.kv.get(f"conv:{GROUP}") == "c1"  # the conversation keeps the turn
    group_text(bot, "quedan 10 raciones")
    assert len(mod.CALLS) == 2
    assert contents(mod.CALLS[1]) == ["[no mention] Marta: quedan 10 raciones"]
    assert ("user", "[no mention] Marta: ¿vienes a cenar, Lucía?") in [
        (h["role"], h["content"]) for h in mod.CALLS[1]["turn"]["history"]
    ]


def test_no_reply_never_reaches_the_chat_inside_an_answer(make_bot):
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = ["Apuntado.\n\n**NO_REPLY**"]
    group_text(bot, "quedan 10 raciones")
    assert bot.tg.texts() == ["Apuntado."]


def test_an_edit_of_an_unmentioned_line_is_handled_like_an_addressed_one(make_bot):
    # MUT: drop `self.remember(...)` in _converse → the edit goes as a new line, not as a check
    bot = make_bot(profile=ALL)
    msg = group_text(bot, "quedan 10 raciones")
    bot.handle({"edited_message": {**msg, "text": "quedan 8 raciones"}})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 2
    assert contents(calls[1]) == [
        (
            "[Marta edited a message you already answered. Check whether your answer still holds "
            "with the new wording, and say what changes.]\nBefore: quedan 10 raciones"
            "\nNow: quedan 8 raciones\nYour answer was: Vale."
        )
    ]


def test_an_edit_of_an_unmentioned_line_answered_with_a_reaction_goes_again(make_bot):
    # MUT: drop the `unmentioned` fallback in on_edit → the edit only rewrites the log
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [REACT]
    msg = group_text(bot, "quedan 10 raciones")
    bot.handle({"edited_message": {**msg, "text": "quedan 8 raciones"}})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 2
    assert contents(calls[1]) == ["[no mention] Marta: quedan 8 raciones"]
    rows = [r for r in bot.kv.chat_rows(GROUP, 0) if r["role"] == "user"]
    assert [(r["text"], r["asked"]) for r in rows] == [("[no mention] Marta: quedan 8 raciones", 1)]


def test_asked_mode_is_unchanged(make_bot):
    # answer_scope: asked (the default), history_scope: all: context only, «[not to you]»
    profile = PROFILE.replace(
        "  admins: {222: Marta}\n", "  admins: {222: Marta}\n  history_scope: all\n"
    )
    bot = make_bot(profile=profile)
    group_text(bot, "quedan 10 raciones")
    assert plugin(bot).CALLS == [] and sent(bot) == []
    say(bot, "hoy poco", reply_to=QUESTION)
    call = plugin(bot).CALLS[-1]
    assert contents(call)[0] == "[not to you] Marta: quedan 10 raciones"
    assert '"[not to you]"' in call["turn"]["rules"] and "[no mention]" not in call["turn"]["rules"]


def test_without_an_assistant_unmentioned_lines_get_no_answer(make_bot):
    bot = make_bot(profile=ALL.split("assistant:")[0])
    group_text(bot, "quedan 10 raciones")
    assert sent(bot) == []  # no «no_ai» for every household line


def test_answer_scope_is_checked(make_bot):
    bot = make_bot()
    assert load_profile(bot.data).answer_scope == "asked"
    bad = PROFILE.replace("  admins: {222: Marta}\n", "  admins: {222: Marta}\n  answer_scope: x\n")
    (bot.data / "cycle" / "profile.yaml").write_text(bad, encoding="utf-8")
    assert "bad_answer_scope" in profile_errors(load_profile(bot.data), bot.data)
    (bot.data / "cycle" / "profile.yaml").write_text(ALL, encoding="utf-8")
    assert profile_errors(load_profile(bot.data), bot.data) == []


def test_the_all_rules_say_how_to_answer_a_line_without_a_mention():
    text = assistant.rules("Lucía", "es", ["Marta"], answer_all=True)
    assert "{unaddressed}" not in text and "{no_reply}" not in text and "{name}" not in text
    assert "every message Lucía or Marta writes reaches you, mention or not" in text
    assert "answer with only a reaction (react: 👍" in text
    assert "write only NO_REPLY" in text
    assert "A mention, a reply to you or a voice note always gets an answer in words, never" in text
    assert '"[not to you]" is context only' in text
    assert "and are context only" not in text  # the asked-mode lines are gone
    assert assistant.drop_no_reply("NO_REPLY") == ""
    assert assistant.drop_no_reply("`NO_REPLY`\n") == ""
    assert assistant.drop_no_reply("Hecho.") == "Hecho."


def test_her_plain_line_is_marked_no_mention_when_telegram_delivers_all_her_messages(make_bot):
    # MUT: let `read_all and role == HER` make her line addressed in all mode → no marker
    bot = make_bot(profile=ALL, read_all=True)
    group_text(bot, "¿vienes a cenar, Marta?", user=HER)
    call = plugin(bot).CALLS[-1]
    assert contents(call) == ["[no mention] Lucía: ¿vienes a cenar, Marta?"]
    say(bot, "me ha bajado", reply_to=QUESTION)  # a reply to the bot is still addressed
    assert contents(plugin(bot).CALLS[-1]) == [
        f"Lucía: [replying to the bot: {QUESTION}] me ha bajado"
    ]


def test_a_stranger_named_like_her_stays_context_and_only_household_lines_are_no_mention(make_bot):
    # MUT: mark the context rows «[no mention]» in all mode → the stranger looks like her
    profile = ALL.replace("  answer_scope: all\n", "  answer_scope: all\n  history_scope: all\n")
    bot = make_bot(profile=profile)
    plugin(bot).REPLIES[:] = ["NO_REPLY"]
    group_text(bot, "ayer sangré mucho, dolor 8", user=STRANGER, first_name="Lucía")
    group_text(bot, "qué tal")
    call = plugin(bot).CALLS[-1]
    assert contents(call) == [
        "[not to you] Lucía: ayer sangré mucho, dolor 8",
        "[no mention] Marta: qué tal",
    ]
    rules = call["turn"]["rules"]
    assert '"[not to you]"' in rules and "context only" in rules


@pytest.mark.parametrize("lang", ["en", "es"])
def test_in_all_mode_the_first_principle_lets_household_lines_without_a_mention_in(lang):
    # MUT: return the shared principles unchanged in all mode → the old principle 1 says
    # "never answer it" and that no rule overrides it
    asked = assistant.rules("Lucía", lang, ["Marta"])
    text = assistant.rules("Lucía", lang, ["Marta"], answer_all=True)
    head = text.split("You are the cycle companion", 1)[0]
    flat = " ".join(head.split())
    old = {
        "en": "The rest of the chat is context: you read it and never answer it.",
        "es": "El resto del chat es contexto: lo lees y nunca lo contestas.",
    }[lang]
    assert old in " ".join(asked.split())  # the shared text, untouched in asked mode
    assert old not in flat
    assert '"[no mention]"' in flat or "«[no mention]»" in flat
    assert head.count("**") == 24  # still twelve principles
    assert head.count("\n1. **") == 1


@pytest.mark.parametrize(
    "answer",
    [
        "NO_REPLY.",
        "(NO_REPLY)",
        '"NO_REPLY"',
        "«NO_REPLY»",
        "[NO_REPLY]",
        "NO\\_REPLY",
        "```\nNO_REPLY\n```",
        "```text\nNO_REPLY\n```",
        " **NO_REPLY** ",
    ],
)
def test_no_reply_with_punctuation_or_wrapping_is_still_silence(answer):
    # MUT: drop the "nothing but punctuation is left" rule in drop_no_reply → "." and "()" leak
    assert assistant.drop_no_reply(answer) == ""


def test_a_fenced_no_reply_sends_nothing(make_bot):
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = ["```\nNO_REPLY\n```"]
    group_text(bot, "¿vienes a cenar, Lucía?")
    assert sent(bot) == []
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_failed_turn_of_only_unmentioned_lines_tells_the_group_nothing(make_bot):
    # MUT: post «ai_down» whatever the lines → the family chat gets the error notice
    bot = make_bot(profile=ALL)
    mod = plugin(bot)
    mod.REPLIES[:] = [RuntimeError("down"), {"text": "x", "conversation": None, "kept": False}]
    group_text(bot, "¿vienes a cenar, Lucía?")
    group_text(bot, "sí, a las nueve", user=HER)
    assert len(mod.CALLS) == 2
    assert sent(bot) == []  # no «ai_down» in the middle of their talk
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 2  # the lines wait for the next turn
    say(bot, "¿me ha bajado?", entities=None, reply_to=QUESTION)
    assert len(mod.CALLS) == 3 and bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_failed_turn_with_an_addressed_line_still_says_so(make_bot):
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [RuntimeError("down")]
    say(bot, "me ha bajado", reply_to=QUESTION)
    assert bot.tg.texts() == [bot.t("ai_down")]


def test_a_busy_plugin_on_unmentioned_lines_says_nothing(make_bot):
    # MUT: tell «ai_busy» whatever the lines → the notice lands in the family chat
    from test_turns import busy

    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [busy(600)]
    group_text(bot, "¿vienes a cenar, Lucía?")
    assert sent(bot) == []
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 1


def test_formatting_that_does_not_wrap_no_reply_stays_balanced():
    # MUT: strip any `*_~ next to the token → "*bold *" loses its closing mark
    assert assistant.drop_no_reply("*bold NO_REPLY*") == "*bold *"
    assert assistant.drop_no_reply("Hecho. **NO_REPLY**") == "Hecho."


def test_a_mention_alone_is_not_an_unmentioned_line(make_bot):
    # MUT: drop the mention check in `unmentioned` → "[no mention] Marta: @bot" starts a turn
    bot = make_bot(profile=ALL)
    msg = message("@example_cycle_bot")
    msg["entities"] = [{"type": "mention", "offset": 0, "length": 18}]
    bot.handle({"message": msg})
    bot.join_idle()
    assert plugin(bot).CALLS == [] and sent(bot) == []


# --- never silent: with answer_scope all, a person's message always shows something -------------


def test_a_reaction_only_answer_goes_on_a_line_that_joined_mid_turn_too(make_bot):
    # MUT: `everyone = None` in _converse → only the last covered message gets the reaction
    bot = make_bot(profile=ALL)
    a, b, late = message("quedan 10 raciones"), message("y 2 yogures", user=HER), message("y pan")
    calls = burst(bot, a, b, reply=REACT, during=late)
    assert len(calls) == 1 and sent(bot) == []
    ids = [a["message_id"], b["message_id"], late["message_id"]]
    assert reactions(bot) == [(m, "👀") for m in ids] + [(m, "👍") for m in ids]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_no_reply_marks_every_covered_message_read(make_bot):
    # MUT: drop `self.look(...)` in _incoming → the line handed over mid-turn shows nothing
    bot = make_bot(profile=ALL)
    a, b, late = message("¿vienes a cenar?"), message("sí, a las 9", user=HER), message("vale")
    calls = burst(bot, a, b, reply="NO_REPLY", during=late)
    assert len(calls) == 1 and sent(bot) == []
    ids = [a["message_id"], b["message_id"], late["message_id"]]
    assert reactions(bot) == [(m, "👀") for m in ids]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


@pytest.mark.parametrize(
    "answer",
    [
        {"text": "", "conversation": "c", "kept": True},  # the private plugin's «(sin respuesta)»
        "",  # a plugin that returns an empty string
        "```telegram\n```",  # an empty gestures block
        "```options\nSí\nNo\n```",  # buttons with no text to carry them
    ],
)
def test_every_answer_that_would_show_nothing_marks_the_lines_read(make_bot, answer):
    # MUT: drop `self.look(...)` in _converse → the empty answer leaves them with nothing
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [answer]
    msg = group_text(bot, "¿compras pan?")
    assert sent(bot) == []
    assert reactions(bot) == [(msg["message_id"], "👀")]


def test_a_record_that_changes_nothing_and_says_nothing_marks_the_line_read(make_bot):
    from test_bot import record

    bot = make_bot(profile=ALL)
    before = bot.repo.log_path.read_bytes()
    plugin(bot).REPLIES[:] = [record("", {"date": "2027-03-03", "flow": "light"})]
    msg = group_text(bot, "el miércoles poco", user=HER)
    assert bot.repo.log_path.read_bytes() == before  # the day already said so
    assert sent(bot) == []
    assert reactions(bot) == [(msg["message_id"], "👀")]


def test_a_record_that_changes_something_shows_anotado_and_no_read_mark(make_bot):
    from test_bot import record

    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [record("", {"date": "2027-03-04", "flow": "heavy"})]
    msg = group_text(bot, "hoy mucho", user=HER)
    assert bot.tg.texts()[0].startswith("Anotado:")
    assert reactions(bot) == [(msg["message_id"], "👀")]  # the one of the start, no other


def test_a_text_answer_leaves_the_eyes_and_adds_no_reactions(make_bot):
    # MUT: call `self.mark_read(...)` whatever was shown, with `fresh` taking every line → a
    # second 👀 under a worded answer
    bot = make_bot(profile=ALL)
    a, b = message("quedan 10 raciones"), message("y 2 yogures", user=HER)
    burst(bot, a, b, reply="Anotado todo.")
    assert bot.tg.texts() == ["Anotado todo."]
    assert reactions(bot) == [(a["message_id"], "👀"), (b["message_id"], "👀")]


def test_a_refused_read_mark_falls_back_to_a_thumb_once_and_never_to_text(make_bot):
    # MUT: drop the `refused` branch in mark_read → the refused messages show nothing at all
    bot = make_bot(profile=ALL)
    a, b = message("¿vienes a cenar?"), message("sí", user=HER)
    bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
    bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
    bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
    burst(bot, a, b, reply="NO_REPLY")
    # the start: both 👀 refused, never a 👍 then; the end: a's 👍 refused, b's taken, once each
    assert reactions(bot) == [
        (a["message_id"], "👀"),
        (b["message_id"], "👀"),
        (a["message_id"], "👍"),
        (b["message_id"], "👍"),
    ]
    assert sent(bot) == []  # never a text instead of 👀


def test_a_mention_never_gets_only_a_read_mark(make_bot):
    # MUT: drop the `asking` branch in mark_read → the reply to the bot gets only 👀
    bot = make_bot(profile=ALL)
    chat, asked = message("¿vienes a cenar?"), addressed("¿me ha bajado?")
    burst(bot, chat, asked, reply="NO_REPLY")
    [said] = sent(bot)
    # MUT: post «send_failed» in mark_read → a false error for a silence the assistant chose
    assert said["text"] == bot.t("read_no_answer") != bot.t("send_failed")
    assert said["reply_parameters"]["message_id"] == asked["message_id"]
    assert reactions(bot) == [(chat["message_id"], "👀"), (asked["message_id"], "👀")]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_mention_answered_with_words_gets_no_read_mark(make_bot):
    bot = make_bot(profile=ALL)
    a, b = message("¿vienes a cenar?"), addressed("¿me ha bajado?")
    burst(bot, a, b, reply="Aún no.")
    assert bot.tg.texts() == ["Aún no."]
    assert reactions(bot) == [(a["message_id"], "👀"), (b["message_id"], "👀")]


def test_a_failed_turn_keeps_its_eyes_and_its_lines_go_with_the_next(make_bot):
    # MUT: delete `pending:<chat>` before tell_down on an exception → the next turn lacks them
    bot = make_bot(profile=ALL)
    mod = plugin(bot)
    mod.REPLIES[:] = [
        RuntimeError("down"),
        {"text": "x", "conversation": None, "kept": False},
        "NO_REPLY",
    ]
    a = group_text(bot, "¿vienes a cenar?")
    b = group_text(bot, "sí", user=HER)
    eyes = [(m["message_id"], "👀") for m in (a, b)]
    assert reactions(bot) == eyes and sent(bot) == []  # the 👀 of the start stay, nothing said
    c = group_text(bot, "vale")
    assert contents(mod.CALLS[2]) == [
        "[no mention] Marta: ¿vienes a cenar?",
        "[no mention] Lucía: sí",
        "[no mention] Marta: vale",
    ]
    assert reactions(bot) == [(m["message_id"], "👀") for m in (a, b, c)]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_busy_turn_keeps_its_eyes_and_says_nothing(make_bot):
    from test_turns import busy

    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [busy(600)]
    msg = group_text(bot, "¿vienes a cenar?")
    assert reactions(bot) == [(msg["message_id"], "👀")] and sent(bot) == []


def test_asked_mode_keeps_the_eyes_of_the_start_and_reacts_on_the_last_only(make_bot):
    # MUT: drop `self.answer_all and` from `people` in _converse → «read_no_answer» in asked mode
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["NO_REPLY"]
    first = say(bot, "hoy poco", reply_to=QUESTION)
    assert sent(bot) == []  # no text, as in 0.8.0
    assert reactions(bot) == [(first["message_id"], "👀")]  # the start's, and no other
    a, b = addressed("hoy poco"), addressed("y dolor 3")
    burst(bot, a, b, reply=REACT)
    assert reactions(bot)[1:] == [(a["message_id"], "👀"), (b["message_id"], "👀")] + [
        (b["message_id"], "👍")  # the answer's reaction replaces the 👀 of the last only
    ]


def test_a_private_chat_in_all_mode_keeps_only_the_eyes_of_the_start(make_bot):
    # MUT: drop `chat == self.chat_id` from `people` in _converse → «read_no_answer» in private
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = ["NO_REPLY"]
    msg = say(bot, "hola", user=ADMIN, chat=ADMIN, private=True)
    assert len(plugin(bot).CALLS) == 1
    assert reactions(bot) == [(msg["message_id"], "👀")] and sent(bot) == []


def test_the_all_rules_say_no_reply_marks_the_messages_read():
    text = " ".join(assistant.rules("Lucía", "es", ["Marta"], answer_all=True).split())
    assert "write only NO_REPLY: the bot then marks those messages with 👀 (read)" in text
    assert "never need words just to say you saw something" in text
    assert "a 👍 says you noted it" in text
    assert "sends nothing" not in text and "gets nothing" not in text
    asked = assistant.rules("Lucía", "es", ["Marta"])
    assert "👀" not in asked.split("You are the cycle companion", 1)[1].split("telegram")[0]


def media(kind, user=HER, caption=None, entities=None, reply_to_bot=False):
    """A sticker, GIF or photo in the group; `entities` go on its caption."""
    extra = (
        {"photo": [{"file_id": "m1", "file_size": 3}]}
        if kind == "photo"
        else {kind: {"file_id": "m1", "is_animated": False, "is_video": False}}
    )
    msg = {
        "message_id": next(_mids),
        "from": {"id": user},
        "chat": {"id": GROUP, "type": "supergroup"},
        **extra,
    }
    if caption is not None:
        msg["caption"] = caption
    if entities is not None:
        msg["caption_entities"] = entities
    if reply_to_bot:
        msg["reply_to_message"] = {"message_id": 1, "from": {"id": BOT_ID}, "text": QUESTION}
    return msg


@pytest.mark.parametrize("kind", ["sticker", "animation", "photo"])
def test_a_sticker_gif_or_photo_left_without_words_is_marked_read_not_an_error(make_bot, kind):
    # MUT: `asks=True` for every file in on_file → «read_no_answer» under a sticker
    bot = make_bot(profile=ALL)
    bot.tg.files["m1"] = b"fictional media"
    plugin(bot).REPLIES[:] = [{"text": "", "conversation": "c", "kept": True}]
    msg = media(kind)
    bot.handle({"message": msg})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1
    assert sent(bot) == []
    assert reactions(bot) == [(msg["message_id"], "👀")]


@pytest.mark.parametrize("how", ["mention", "reply"])
def test_a_photo_that_calls_the_bot_still_gets_words(make_bot, how):
    # MUT: `asks=False` for every file in on_file → the photo that asked gets only 👀
    bot = make_bot(profile=ALL)
    bot.tg.files["m1"] = b"fictional media"
    plugin(bot).REPLIES[:] = ["NO_REPLY"]
    if how == "mention":
        mention = [{"type": "mention", "offset": 0, "length": 18}]
        msg = media("photo", caption="@example_cycle_bot mira", entities=mention)
    else:
        msg = media("photo", caption="mira", reply_to_bot=True)
    bot.handle({"message": msg})
    bot.join_idle()
    [said] = sent(bot)
    assert said["text"] == bot.t("read_no_answer")
    assert said["reply_parameters"]["message_id"] == msg["message_id"]
    assert reactions(bot) == [(msg["message_id"], "👀")]  # the start's, no second one


def test_a_voice_note_left_without_words_gets_words(make_bot):
    # MUT: `"asks": False` on the voice line → the voice note gets only 👀
    from test_bot import voice

    bot = make_bot(profile=ALL)
    bot.tg.files["v1"] = b"OggS fictional"
    plugin(bot).VOICES[:] = ["me ha bajado hoy"]
    plugin(bot).REPLIES[:] = ["NO_REPLY"]
    msg = voice(bot, mime_type="audio/ogg")
    echo, said = sent(bot)
    assert echo["text"] == "🎤 me ha bajado hoy"
    assert said["text"] == bot.t("read_no_answer")
    assert said["reply_parameters"]["message_id"] == msg["message_id"]
    assert reactions(bot) == [(msg["message_id"], "👀")]  # on the note itself, not the echo


def test_a_line_queued_by_0_8_without_asks_still_counts_by_its_mark():
    from supercycletracker.bot import _asking

    lines = [
        {"id": 1, "content": "[no mention] Marta: hola"},
        {"id": 2, "content": "Lucía [replying to the bot: ¿Te ha bajado?] sí"},
        {"id": 3, "content": "Lucía: [file: inbox/3-sticker.webp]", "asks": False},
    ]
    assert _asking(lines) == [2]


def test_an_unchanged_record_with_a_gesture_that_fails_still_marks_the_line_read(make_bot):
    # MUT: `shown = texted` at the start of act → the failed sticker counts as shown
    from test_bot import record

    bot = make_bot(profile=ALL)
    before = bot.repo.log_path.read_bytes()
    answer = record("```telegram\nsticker: 🥰\n```", {"date": "2027-03-03", "flow": "light"})
    plugin(bot).REPLIES[:] = [answer]
    msg = group_text(bot, "el miércoles poco", user=HER)
    assert bot.repo.log_path.read_bytes() == before  # the day already said so
    assert bot.tg.of("sendSticker") == [] and sent(bot) == []
    assert reactions(bot) == [(msg["message_id"], "👀")]


def test_an_edit_that_adds_a_mention_to_an_answered_line_gets_words_when_left_silent(make_bot):
    # MUT: `"asks": False` on the edit line in on_edit → the edit that calls the bot gets 👀
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = ["Vale.", "NO_REPLY"]
    msg = group_text(bot, "quedan 10 raciones")
    mention = [{"type": "mention", "offset": 0, "length": 18}]
    text = "@example_cycle_bot quedan 8 raciones"
    bot.handle({"edited_message": {**msg, "text": text, "entities": mention}})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 2
    said = sent(bot)[-1]
    assert said["text"] == bot.t("read_no_answer")
    assert said["reply_parameters"]["message_id"] == msg["message_id"]
    assert reactions(bot) == [(msg["message_id"], "👀")]  # once, at the first turn's start


def test_a_plain_edit_of_an_answered_line_left_silent_is_marked_read(make_bot):
    # MUT: `_asking` keeps the 0.8 rule for edit lines → the plain edit gets «read_no_answer»
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = ["Vale.", "NO_REPLY"]
    msg = group_text(bot, "quedan 10 raciones")
    bot.handle({"edited_message": {**msg, "text": "quedan 8 raciones"}})
    bot.join_idle()
    assert bot.tg.texts() == ["Vale."]
    assert reactions(bot) == [(msg["message_id"], "👀")]
