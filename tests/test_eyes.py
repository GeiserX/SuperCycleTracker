"""👀 the moment the bot receives a message a turn will answer, and again (a no-op) when the turn
starts: every person's message shows it at once, in every chat, so nobody waits on a long turn
wondering whether the bot saw them. It goes on its own thread, never on bot messages or taps, never
twice, and a refusal never touches the handler or the turn."""

import threading
import time

import pytest
from conftest import ADMIN, GROUP, HER, PROFILE
from test_answer_scope import ALL, REACT, addressed, contents, media, message, reactions, sent
from test_bot import QUESTION, buttons, plugin, say, tap

MENTION = [{"type": "mention", "offset": 0, "length": 18}]
# `answer_scope: asked` with the whole group as context: plain lines are logged, never answered
PRIVACY_OFF = PROFILE.replace(
    "  admins: {222: Marta}\n", "  admins: {222: Marta}\n  history_scope: all\n"
)


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


# --- on arrival ------------------------------------------------------------------------------


def slow_reactions(bot, first_only=False):
    """Hold every `setMessageReaction` (or only the first) until the returned Event is set."""
    release, call, calls = threading.Event(), bot.tg.call, []

    def slow(method, **params):
        if method == "setMessageReaction":
            calls.append(params)
            if not first_only or len(calls) == 1:
                release.wait(10)
        return call(method, **params)

    bot.tg.call = slow
    return release


def held_turn(bot):
    """The next turn's plugin call waits until `go` is set; `started` is set once it runs."""
    started, go = threading.Event(), threading.Event()

    def answer(turn):
        started.set()
        go.wait(10)
        return "Vale."

    plugin(bot).REPLIES[:] = [answer]
    return started, go


def test_a_photo_sent_during_a_running_turn_shows_eyes_before_that_turn_ends(make_bot):
    # MUT: drop `self.look(chat, [line])` in queue_line → the photo waits for its own turn
    bot = make_bot(profile=ALL)
    bot.tg.files["m1"] = b"fictional media"
    started, go = held_turn(bot)
    first = message("¿compras pan?")
    bot.handle({"message": first})
    assert started.wait(5)
    pic = media("photo")
    bot.handle({"message": pic})  # a file never joins a running turn: it waits for the next
    try:
        assert eyes_on(bot, pic["message_id"], timeout=2)
        assert bot.tg.texts() == [] and len(plugin(bot).CALLS) == 1  # the first turn still runs
    finally:
        go.set()
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 2
    assert sorted(reactions(bot)) == sorted(
        [(first["message_id"], "👀"), (pic["message_id"], "👀")]
    )


def test_a_line_arriving_mid_turn_shows_eyes_before_the_plugin_takes_it(make_bot):
    # MUT: drop `self.look(chat, [line])` in queue_line → no 👀 until `incoming` takes it
    bot = make_bot(profile=ALL)
    first, late = message("quedan 10 raciones"), message("y pan", user=HER)
    seen = []

    def handover(turn):
        bot.handle({"message": late})
        seen.append(eyes_on(bot, late["message_id"], timeout=2))  # not taken yet
        got = turn["incoming"]()
        return {"text": "Anotado.", "conversation": "c", "kept": True, "covers": [got[0]["id"]]}

    plugin(bot).REPLIES[:] = [handover]
    bot.handle({"message": first})
    bot.join_idle()
    assert seen == [True]
    assert reactions(bot) == [(first["message_id"], "👀"), (late["message_id"], "👀")]
    assert len(plugin(bot).CALLS) == 1


def private_line():
    msg = message("hola", user=HER)
    msg["chat"] = {"id": HER, "type": "private"}
    return msg


def voice_note():
    return {
        "message_id": 70_001,
        "from": {"id": HER},
        "chat": {"id": GROUP, "type": "supergroup"},
        "voice": {"file_id": "v1", "duration": 4},
    }


ARRIVALS = {
    "a private chat": private_line,
    "asked, a mention": lambda: mentioned("¿me ha bajado?"),
    "asked, a reply to the bot": lambda: addressed("hoy poco"),
    "asked, a voice note": voice_note,
    "asked, a photo": lambda: media("photo"),
}


@pytest.mark.parametrize("case", list(ARRIVALS))
def test_eyes_land_on_arrival_before_the_turn_starts(make_bot, case):
    # MUT: drop `self.look(chat, [line])` in queue_line → no 👀 while the debounce runs
    bot = make_bot()  # `answer_scope: asked`
    bot.tg.files["v1"] = b"OggS fictional"
    bot.tg.files["m1"] = b"fictional media"
    plugin(bot).VOICES[:] = ["me ha bajado"]
    bot.debounce_s = 30  # the turn cannot start while the test looks
    msg = ARRIVALS[case]()
    bot.handle({"message": msg})
    try:
        assert eyes_on(bot, msg["message_id"], timeout=2)
        assert plugin(bot).CALLS == []
    finally:
        bot.debounce_s = 0
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1
    assert reactions(bot) == [(msg["message_id"], "👀")]  # once, never on the bot's echo


def test_an_edit_that_now_calls_the_bot_gets_eyes_on_arrival(make_bot):
    # MUT: drop `self.look(chat, [line])` in queue_line → the edit's message waits for its turn
    bot = make_bot(profile=PRIVACY_OFF)
    plain = message("quedan 10 raciones", user=HER)
    bot.handle({"message": plain})
    bot.join_idle()
    assert reactions(bot) == [] and plugin(bot).CALLS == []  # context: never answered
    bot.debounce_s = 30
    edited = {**plain, "text": "@example_cycle_bot quedan 8 raciones", "entities": MENTION}
    bot.handle({"edited_message": edited})
    try:
        assert eyes_on(bot, plain["message_id"], timeout=2)
        assert plugin(bot).CALLS == []
    finally:
        bot.debounce_s = 0
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1
    assert reactions(bot) == [(plain["message_id"], "👀")]


def test_an_asked_mode_context_line_gets_no_eyes(make_bot):
    # MUT: call `self.look` in log_seen → 👀 on a line the bot never answers
    bot = make_bot(profile=PRIVACY_OFF)
    plain = message("¿alguien compra pan?")
    bot.handle({"message": plain})
    bot.join_idle()
    asked = mentioned("¿me ha bajado?")
    bot.handle({"message": asked})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1
    assert reactions(bot) == [(asked["message_id"], "👀")]


def test_a_slow_arrival_eyes_never_holds_the_handler(make_bot):
    # MUT: run the 👀 job inline in `look` instead of `self.eyes.submit` → handle() waits on it
    bot = make_bot(profile=ALL)
    release = slow_reactions(bot)
    msg = message("¿compras pan?")
    try:
        started = time.monotonic()
        bot.handle({"message": msg})
        assert time.monotonic() - started < 1
    finally:
        release.set()
    bot.join_idle()
    assert bot.tg.texts() == ["Vale."]
    assert reactions(bot) == [(msg["message_id"], "👀")]


def test_an_arrival_eyes_on_its_way_lands_before_a_reaction_only_answer(make_bot):
    # MUT: drop `state.looks.append(was)` in `_look` → the 👍 lands first and the 👀 covers it
    bot = make_bot(profile=ALL)
    release = slow_reactions(bot, first_only=True)
    answered = threading.Event()

    def answer(turn):
        answered.set()
        return REACT

    plugin(bot).REPLIES[:] = [answer]
    msg = message("quedan 10 raciones")
    bot.handle({"message": msg})
    assert answered.wait(5)
    time.sleep(0.3)  # time for an end of turn that did not wait to put its 👍
    release.set()
    bot.join_idle()
    assert reactions(bot) == [(msg["message_id"], "👀"), (msg["message_id"], "👍")]


# --- the 👀 never stands in for the answer ----------------------------------------------------


def test_a_photo_marked_during_a_running_turn_still_gets_its_answer_in_its_own_turn(make_bot):
    # MUT: `_turn` skips lines whose message already shows 👀 → the photo is never answered
    bot = make_bot(profile=ALL)
    bot.tg.files["m1"] = b"fictional media"
    started, go = held_turn(bot)
    first = message("¿compras pan?")
    bot.handle({"message": first})
    assert started.wait(5)
    pic = media("photo")
    bot.handle({"message": pic})
    assert eyes_on(bot, pic["message_id"], timeout=2)
    plugin(bot).REPLIES.append(REACT)  # its own turn answers with a reaction only
    go.set()
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 2
    assert [f"inbox/{pic['message_id']}-foto.jpg" in c for c in contents(calls[1])] == [True]
    assert bot.tg.texts() == ["Vale."]
    assert reactions(bot)[-2:] == [(pic["message_id"], "👀"), (pic["message_id"], "👍")]


def test_a_line_marked_mid_turn_that_the_plugin_never_took_gets_its_own_answer(make_bot):
    # MUT: `_turn` skips lines whose message already shows 👀 → the late line is never answered
    bot = make_bot(profile=ALL)
    first, late = message("quedan 10 raciones"), message("¿y pan?", user=HER)

    def ignores(turn):
        bot.handle({"message": late})  # never handed over: `incoming` is not called
        assert eyes_on(bot, late["message_id"], timeout=2)
        return "Vale."

    plugin(bot).REPLIES[:] = [ignores, "Sí, compro pan."]
    bot.handle({"message": first})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 2
    assert [c.endswith("¿y pan?") for c in contents(calls[1])] == [True]
    assert bot.tg.texts() == ["Vale.", "Sí, compro pan."]
    assert reactions(bot) == [(first["message_id"], "👀"), (late["message_id"], "👀")]


def test_a_message_marked_on_arrival_whose_turn_failed_gets_its_answer_on_the_retry(make_bot):
    # MUT: act reacts only on messages without the bot's 👀 → the retry's answer reaches nobody
    bot = make_bot(profile=ALL)
    plugin(bot).REPLIES[:] = [RuntimeError("down"), REACT]
    a = message("quedan 10 raciones")
    bot.handle({"message": a})
    bot.join_idle()  # failed, quietly: the line stays with its 👀
    assert reactions(bot) == [(a["message_id"], "👀")] and sent(bot) == []
    b = message("y 2 de pan", user=HER)
    bot.handle({"message": b})
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert len(calls) == 2
    assert [c.rsplit(": ", 1)[1] for c in contents(calls[1])] == [
        "quedan 10 raciones",
        "y 2 de pan",
    ]
    assert reactions(bot)[2:] == [(a["message_id"], "👍"), (b["message_id"], "👍")]


def test_a_message_marked_before_a_restart_still_gets_its_answer_after_it(make_bot, data_repo):
    # MUT: a line marked on arrival is stored as handled and `_turn` skips it → no answer
    from test_turns import _restart

    bot = make_bot()
    bot.worker.stop()  # the process dies before the turn starts
    msg = addressed("¿me toca ya?")
    bot.handle({"message": msg})
    assert eyes_on(bot, msg["message_id"])
    new = _restart(bot, data_repo)
    try:
        assert len(plugin(new).CALLS) == 1
        assert new.tg.texts() == ["Vale."]
        assert new.kv.get_json(f"pending:{GROUP}") is None
    finally:
        new.close()
