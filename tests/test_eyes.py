"""👀 the moment a turn starts: every person's message the turn covers shows it at once, in every
chat, so nobody waits on a long turn wondering whether the bot saw them. It goes on its own thread,
never on bot messages or taps, never twice, and a refusal never touches the turn."""

import threading
import time

import pytest
from conftest import ADMIN, HER
from test_answer_scope import ALL, REACT, addressed, message, reactions, sent
from test_bot import QUESTION, buttons, plugin, say, tap

MENTION = [{"type": "mention", "offset": 0, "length": 18}]


def eyes_on(bot, mid, timeout=5.0):
    """Whether 👀 reaches message `mid` within `timeout` (it goes on its own thread)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (mid, "👀") in reactions(bot):
            return True
        time.sleep(0.01)
    return False


def private(text, user=HER):
    msg = message(text, user=user)
    msg["chat"] = {"id": user, "type": "private"}
    return msg


def mentioned(text):
    msg = message(f"@example_cycle_bot {text}", user=HER)
    msg["entities"] = MENTION
    return msg


CASES = {
    "all, a plain line": (ALL, lambda: message("¿compras pan?")),
    "all, a mention": (ALL, lambda: mentioned("¿me ha bajado?")),
    "asked, a reply to the bot": (None, lambda: addressed("hoy poco")),
    "asked, a private chat": (None, lambda: private("hola")),
    "all, a private chat": (ALL, lambda: private("hola", user=ADMIN)),
}


@pytest.mark.parametrize("case", list(CASES))
def test_eyes_land_when_the_turn_starts_before_the_ai_answers(make_bot, case):
    # MUT: move `self.look(...)` in _converse below the plugin call → no 👀 while the AI works
    profile, make = CASES[case]
    bot = make_bot(profile=profile) if profile else make_bot()
    msg = make()
    seen = []

    def answer(turn):
        seen.append(eyes_on(bot, msg["message_id"]))  # the AI has not answered yet
        return "Vale."

    plugin(bot).REPLIES[:] = [answer]
    bot.handle({"message": msg})
    bot.join_idle()
    assert seen == [True]
    assert bot.tg.texts() == ["Vale."]
    assert reactions(bot) == [(msg["message_id"], "👀")]  # a text answer leaves it there


def test_a_line_that_joins_mid_turn_gets_eyes_at_once(make_bot):
    # MUT: drop `self._look(...)` in _incoming → the late line shows nothing until the end
    bot = make_bot(profile=ALL)
    first, late = message("quedan 10 raciones"), message("y pan", user=HER)
    seen = []

    def handover(turn):
        bot.handle({"message": late})
        got = turn["incoming"]()
        seen.append(eyes_on(bot, late["message_id"]))  # the turn is still running
        return {"text": "Anotado.", "conversation": "c", "kept": True, "covers": [got[0]["id"]]}

    plugin(bot).REPLIES[:] = [handover]
    bot.handle({"message": first})
    bot.join_idle()
    assert seen == [True]
    assert reactions(bot) == [(first["message_id"], "👀"), (late["message_id"], "👀")]


def test_a_slow_eyes_call_never_holds_the_turn(make_bot):
    # MUT: run the 👀 job inline in `look` instead of `self.eyes.submit` → the AI waits on it
    bot = make_bot(profile=ALL)
    release, called = threading.Event(), threading.Event()
    call = bot.tg.call

    def slow(method, **params):
        if method == "setMessageReaction":
            release.wait(10)
        return call(method, **params)

    bot.tg.call = slow

    def answer(turn):
        called.set()
        return "Vale."

    plugin(bot).REPLIES[:] = [answer]
    msg = message("¿compras pan?")
    bot.handle({"message": msg})
    try:
        assert called.wait(5)  # the AI got the turn while Telegram still held the 👀
    finally:
        release.set()
    bot.join_idle()
    assert bot.tg.texts() == ["Vale."]
    assert reactions(bot) == [(msg["message_id"], "👀")]


def test_a_refused_start_eyes_never_blocks_the_turn_or_posts_text(make_bot):
    # MUT: `dict.fromkeys((READ, NOTED))` tries in the 👀 job → a 👍 («noted») at the start
    bot = make_bot()
    bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
    msg = say(bot, "hoy poco", reply_to=QUESTION)
    assert len(plugin(bot).CALLS) == 1
    assert bot.tg.texts() == ["Vale."]  # the answer, and no word about the refusal
    assert reactions(bot) == [(msg["message_id"], "👀")]  # the refused try, then nothing


def test_bot_messages_get_no_eyes(make_bot):
    # MUT: `self.unmentioned` takes bots → the bot's line starts a turn and gets 👀
    bot = make_bot(profile=ALL)
    other = message("Recordatorio: pastilla", user=ADMIN, is_bot=True)  # a bot listed as admin
    bot.handle({"message": other})
    bot.join_idle()
    assert plugin(bot).CALLS == [] and reactions(bot) == []
    msg = message("¿compras pan?")
    bot.handle({"message": msg})
    bot.join_idle()
    assert reactions(bot) == [(msg["message_id"], "👀")]


def test_a_turn_of_only_a_tap_puts_no_eyes_on_the_bots_message(make_bot):
    # MUT: drop `not x.get("tap")` from `_people` → 👀 on the bot's own question
    bot = make_bot()
    mod = plugin(bot)
    mod.REPLIES[:] = ["¿Cuánto?\n\n```options\n- Poco\n- Mucho\n```", "Apuntado."]
    first = say(bot, "me ha bajado", reply_to=QUESTION)
    post = sent(bot)[-1]
    question = 1001 + len(sent(bot)) - 1  # FakeTelegram's ids count up from 1001
    tap(bot, buttons(post["reply_markup"])[0], message_id=question)
    bot.join_idle()
    assert len(mod.CALLS) == 2  # the tap ran a turn of its own, covering no person's message
    assert reactions(bot) == [(first["message_id"], "👀")]


def test_a_message_never_gets_two_eyes(make_bot):
    # MUT: drop the `not in self._eyed` filter in `_look` → a second 👀 on each kept line
    bot = make_bot(profile=ALL)
    mod = plugin(bot)
    mod.REPLIES[:] = [RuntimeError("down"), "NO_REPLY", "Vale.", "NO_REPLY"]
    a = message("¿vienes a cenar?")
    bot.handle({"message": a})
    bot.join_idle()  # failed: the 👀 stays and the line waits
    b = message("sí", user=HER)
    bot.handle({"message": b})
    bot.join_idle()  # both go, silent: the end adds nothing
    c = message("quedan 10 raciones")
    bot.handle({"message": c})
    bot.join_idle()
    bot.handle({"edited_message": {**c, "text": "quedan 8 raciones"}})
    bot.join_idle()  # the edit's turn covers the same message: its 👀 is already there
    assert len(mod.CALLS) == 4
    assert reactions(bot) == [(m["message_id"], "👀") for m in (a, b, c)]


def test_a_reaction_only_answer_replaces_the_eyes_and_a_later_turn_shows_them_again(make_bot):
    # MUT: drop `self.unlook(...)` in act → the edit's turn shows no 👀, the 👍 stays meanwhile
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [REACT, "Vale."]
    msg = message("quedan 10 raciones")
    bot.handle({"message": msg})
    bot.join_idle()
    assert sent(bot) == []
    assert reactions(bot) == [(msg["message_id"], "👀"), (msg["message_id"], "👍")]
    bot.handle({"edited_message": {**msg, "text": "quedan 8 raciones"}})
    bot.join_idle()
    assert bot.tg.texts() == ["Vale."]
    assert reactions(bot)[2:] == [(msg["message_id"], "👀")]


def test_forgetting_old_eyes_never_drops_one_still_on_its_way(make_bot, monkeypatch):
    # MUT: `self._eyed.popitem(last=False)` in `_look` → the queued 👀 is forgotten and skipped
    from supercycletracker import bot as module

    monkeypatch.setattr(module, "EYED_MAX", 1)
    bot = make_bot(profile=ALL)
    release = threading.Event()
    call = bot.tg.call

    def slow(method, **params):
        if method == "setMessageReaction":
            release.wait(10)
        return call(method, **params)

    bot.tg.call = slow
    a, b = message("quedan 10 raciones"), message("y pan", user=HER)
    bot.debounce_s = 0.3
    bot.handle({"message": a})
    bot.handle({"message": b})  # one turn, two claims on their way, room for one
    time.sleep(0.5)
    release.set()
    bot.join_idle()
    assert reactions(bot)[:2] == [(a["message_id"], "👀"), (b["message_id"], "👀")]
