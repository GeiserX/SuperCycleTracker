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


def test_a_burst_of_unmentioned_lines_is_one_turn_and_a_reaction_goes_on_the_last(make_bot):
    # MUT: `_mid(covered[0])` instead of `_mid(covered[-1])` in the act call → reaction on the first
    bot = make_bot(profile=ALL)
    bot.debounce_s = 2.5
    second = message("y he sacado 100 g de merluza", user=HER)
    fake_time(bot, {1.0: lambda: bot.handle({"message": second})})
    plugin(bot).REPLIES[:] = [REACT]
    bot.handle({"message": message("quedan 10 raciones de lasaña")})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 1
    assert contents(calls[0]) == [
        "[no mention] Marta: quedan 10 raciones de lasaña",
        "[no mention] Lucía: y he sacado 100 g de merluza",
    ]
    assert sent(bot) == []  # a reaction only: no text
    [r] = bot.tg.of("setMessageReaction")
    assert r["message_id"] == second["message_id"]
    assert r["reaction"] == [{"type": "emoji", "emoji": "👍"}]
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


def test_no_reply_sends_nothing_raises_nothing_and_counts_as_answered(make_bot):
    # MUT: drop `drop_no_reply` in _converse → "NO_REPLY" is sent to the chat
    bot = make_bot(profile=ALL)
    mod = plugin(bot)
    mod.REPLIES[:] = [{"text": "NO_REPLY", "conversation": "c1", "kept": True}]
    group_text(bot, "¿vienes a cenar, Lucía?")
    assert len(mod.CALLS) == 1
    assert sent(bot) == []  # no text, no «ai_down», no «try again»
    assert bot.tg.of("setMessageReaction") == []
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
    assert "A mention or a reply to you always gets an answer in words." in text
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
