import os
import shutil
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from supercycletracker import assistant, records
from supercycletracker.assistant import BAD_BLOCK, PluginError
from supercycletracker.i18n import Translator
from supercycletracker.records import SYMPTOMS

FIXTURES = Path(__file__).parent / "fixtures"
MADRID = ZoneInfo("Europe/Madrid")


@pytest.fixture
def data(tmp_path) -> Path:
    (tmp_path / "plugins").mkdir()
    shutil.copy(FIXTURES / "fake_plugin.py", tmp_path / "plugins" / "fake.py")
    return tmp_path


# --- loading -------------------------------------------------------------------------------


def test_the_plugin_must_resolve_inside_the_data_directory(data, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    shutil.copy(FIXTURES / "fake_plugin.py", outside / "evil.py")
    rel = os.path.relpath(outside / "evil.py", data)
    with pytest.raises(PluginError, match="plugin_outside_data"):
        assistant.load_module(data, rel)
    (data / "plugins" / "link.py").symlink_to(outside / "evil.py")
    with pytest.raises(PluginError, match="plugin_outside_data"):
        assistant.load_module(data, "plugins/link.py")
    assert assistant.load_module(data, "plugins/fake.py").converse  # positive control


def test_a_missing_or_unset_plugin_is_a_reason_code(data):
    with pytest.raises(PluginError, match="plugin_missing"):
        assistant.load_module(data, "plugins/nope.py")
    with pytest.raises(PluginError, match="no_plugin"):
        assistant.load_module(data, None)


def test_a_plugin_that_fails_to_import_says_so_without_its_text(data):
    (data / "plugins" / "broken.py").write_text("raise RuntimeError('secret words')\n")
    with pytest.raises(PluginError) as info:
        assistant.load_module(data, "plugins/broken.py")
    assert info.value.code == "plugin_load_failed"
    assert "secret" not in str(info.value)
    origins = [getattr(m, "__file__", "") or "" for m in list(sys.modules.values())]
    assert not [o for o in origins if o.endswith("broken.py")]  # a failed import leaves nothing


DATACLASS_PLUGIN = """\
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class State:
    calls: int = 0


STATE = State()


def converse(turn, settings):
    STATE.calls += 1
    return str(STATE.calls)
"""


def test_a_plugin_may_keep_its_state_in_a_dataclass(data):
    (data / "plugins" / "state.py").write_text(DATACLASS_PLUGIN)
    turn = assistant.turn(None, "r", "c", "", ["x"])
    assert assistant.converse(data, "plugins/state.py", turn, {"timeout_s": 5}).text == "1"
    assert assistant.converse(data, "plugins/state.py", turn, {"timeout_s": 5}).text == "2"


def test_the_module_is_kept_per_mtime_and_reloaded_after_an_edit(data):
    first = assistant.load_module(data, "plugins/fake.py")
    first.CALLS.append("state")
    assert assistant.load_module(data, "plugins/fake.py") is first  # state survives
    path = data / "plugins" / "fake.py"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = assistant.load_module(data, "plugins/fake.py")
    assert second is not first
    assert second.CALLS == []


def test_settings_leave_out_plugin_and_instructions():
    block = {"plugin": "plugins/x.py", "instructions": "be kind", "timeout_s": 600, "model": None}
    assert assistant.settings(block) == {"timeout_s": 600, "model": None}
    assert assistant.settings(None) == {}
    assert assistant.timeout_s({}) == assistant.DEFAULT_TIMEOUT_S
    assert assistant.timeout_s({"timeout_s": 30}) == 30


# --- calling -------------------------------------------------------------------------------


def test_converse_passes_the_turn_and_settings_and_reads_the_reply(data):
    mod = assistant.load_module(data, "plugins/fake.py")
    mod.REPLIES[:] = ["plain", {"text": "kept", "conversation": "c-1", "kept": True}]
    turn = assistant.turn(None, "rules", "context", "", ["Lucía: hola"])
    assert assistant.converse(data, "plugins/fake.py", turn, {"timeout_s": 5}) == assistant.Reply(
        "plain"
    )
    second = assistant.converse(data, "plugins/fake.py", turn, {"timeout_s": 5})
    assert second == assistant.Reply("kept", "c-1", True)
    assert mod.CALLS[0] == {"turn": turn, "settings": {"timeout_s": 5}}


def test_a_reply_that_breaks_the_contract_is_refused():
    for bad in (
        None,
        3,
        {"text": 1},
        {"text": "x", "kept": "no"},
        {"text": "x", "conversation": 4},
        {"text": "x", "later": "not a function"},
    ):
        with pytest.raises(PluginError, match="bad_reply"):
            assistant.reply(bad)
    assert assistant.reply({"text": "x", "kept": False}) == assistant.Reply("x", None, False)


def test_a_reply_may_carry_later():
    def job():
        return "hecho"

    out = assistant.reply({"text": "Me pongo.", "conversation": "c", "later": job})
    assert out == assistant.Reply("Me pongo.", "c", True, job)
    assert assistant.reply({"text": "x", "later": None}).later is None


def test_transcribe_returns_the_plugins_text(data):
    mod = assistant.load_module(data, "plugins/fake.py")
    mod.VOICES[:] = ["me ha bajado hoy"]
    out = assistant.transcribe(data, "plugins/fake.py", b"ogg", "audio/ogg", "es", {"timeout_s": 5})
    assert out == "me ha bajado hoy"
    assert mod.HEARD == [(b"ogg", "audio/ogg", "es")]


def test_transcribe_is_optional_and_a_provider_may_refuse_it(data):
    with pytest.raises(NotImplementedError):  # the fake with nothing scripted cannot listen
        assistant.transcribe(data, "plugins/fake.py", b"x", "audio/ogg", "es", {})
    (data / "plugins" / "bare.py").write_text("def converse(turn, settings):\n    return 'x'\n")
    with pytest.raises(NotImplementedError):  # no transcribe at all
        assistant.transcribe(data, "plugins/bare.py", b"x", "audio/ogg", "es", {})
    with pytest.raises(NotImplementedError):  # no plugin configured
        assistant.transcribe(data, None, b"x", "audio/ogg", "es", {})


def test_transcribe_refuses_what_is_not_text_and_passes_errors_up(data):
    mod = assistant.load_module(data, "plugins/fake.py")
    mod.VOICES[:] = [None, KeyError("boom")]
    with pytest.raises(PluginError, match="bad_transcript"):
        assistant.transcribe(data, "plugins/fake.py", b"x", "audio/ogg", "es", {})
    with pytest.raises(KeyError):
        assistant.transcribe(data, "plugins/fake.py", b"x", "audio/ogg", "es", {})


def test_a_plugin_that_outlives_its_time_is_a_timeout(data):
    mod = assistant.load_module(data, "plugins/fake.py")
    release = threading.Event()
    mod.REPLIES[:] = [lambda turn: release.wait(5) and "late"]
    turn = assistant.turn(None, "r", "c", "", ["x"])
    try:
        with pytest.raises(PluginError, match="timeout"):
            assistant.converse(data, "plugins/fake.py", turn, {"timeout_s": 0.2})
    finally:
        release.set()


def test_a_note_that_outlives_its_time_does_not_block_a_turn(data):
    """Notes are heard on their own thread: an abandoned transcribe keeps transcribe busy, never
    converse."""
    mod = assistant.load_module(data, "plugins/fake.py")
    release = threading.Event()
    mod.VOICES[:] = [lambda: release.wait(5) and "tarde"]
    try:
        with pytest.raises(PluginError, match="timeout"):
            assistant.transcribe(
                data, "plugins/fake.py", b"x", "audio/ogg", "es", {"timeout_s": 0.2}
            )
        turn = assistant.turn(None, "r", "c", "", ["x"])
        assert assistant.converse(data, "plugins/fake.py", turn, {"timeout_s": 5}).text == "Vale."
        with pytest.raises(PluginError, match="plugin_busy"):
            assistant.transcribe(data, "plugins/fake.py", b"x", "audio/ogg", "es", {"timeout_s": 5})
    finally:
        release.set()


def test_a_call_that_ends_keeps_the_guard_of_one_abandoned_beside_it(data):
    """Two answers to asks may run at once (a decline from the plugin's thread, a tap from the
    asks thread). The one that ends must not clear the marker of the one abandoned meanwhile."""
    mod = assistant.load_module(data, "plugins/fake.py")
    slow_in, abandoned, release = threading.Event(), threading.Event(), threading.Event()

    def slow(*_):
        slow_in.set()
        release.wait(5)
        return {"ok": True}

    def fast(*_):
        abandoned.wait(5)  # still running when the slow one is abandoned, ends after it
        return {"ok": True}

    calls = iter([slow, fast])
    mod.answer = lambda request, decision, settings: next(calls)()
    out = {}

    def tap():
        out["fast"] = assistant.answer(data, "plugins/fake.py", {}, {}, {"timeout_s": 5})

    first = threading.Thread(target=lambda: _abandon(data, out, abandoned))
    first.start()
    assert slow_in.wait(5)
    second = threading.Thread(target=tap)
    second.start()
    first.join(5)
    second.join(5)
    try:
        assert out == {"slow": "timeout", "fast": {"ok": True, "error": None}}
        with pytest.raises(PluginError, match="plugin_busy"):  # the slow one still runs
            assistant.answer(data, "plugins/fake.py", {}, {}, {"timeout_s": 5})
    finally:
        release.set()


def _abandon(data, out, abandoned):
    try:
        assistant.answer(data, "plugins/fake.py", {}, {}, {"timeout_s": 0.3})
    except PluginError as e:
        out["slow"] = e.code
    abandoned.set()


def test_the_plugins_own_exception_goes_up_as_it_is(data):
    mod = assistant.load_module(data, "plugins/fake.py")
    mod.REPLIES[:] = [KeyError("boom")]
    with pytest.raises(KeyError):
        assistant.converse(data, "plugins/fake.py", assistant.turn(None, "r", "c", "", []), {})


@pytest.mark.parametrize("exit_", [SystemExit(1), KeyboardInterrupt()])
def test_a_plugin_that_exits_is_a_reason_code_not_an_exit(data, exit_):
    mod = assistant.load_module(data, "plugins/fake.py")
    mod.REPLIES[:] = [exit_]
    with pytest.raises(PluginError, match="plugin_aborted"):
        assistant.converse(data, "plugins/fake.py", assistant.turn(None, "r", "c", "", []), {})


def test_forget_is_optional(data):
    mod = assistant.load_module(data, "plugins/fake.py")
    assistant.forget(data, "plugins/fake.py", "c-9", {})
    assert mod.FORGOTTEN == ["c-9"]
    (data / "plugins" / "bare.py").write_text("def converse(turn, settings):\n    return 'x'\n")
    assistant.forget(data, "plugins/bare.py", "c-9", {})  # no forget: nothing happens


# --- the turn ------------------------------------------------------------------------------


def test_the_turn_has_the_contract_shape():
    turn = assistant.turn("c-1", "R", "C", '{"date": "x"}\n', ["Lucía: uno", "Marta: dos"])
    assert turn == {
        "conversation": "c-1",
        "rules": "R",
        "context": "C",
        "files": {"cycle/log.jsonl": '{"date": "x"}\n'},
        "new": [
            {"role": "user", "content": "Lucía: uno"},
            {"role": "user", "content": "Marta: dos"},
        ],
        "history": [],
    }


def test_rules_are_filled_and_keep_the_block_example():
    text = assistant.rules("Lucía", "es", ["Marta"], "Prefer short answers.")
    for placeholder in ("{name}", "{language_name}", "{others}", "{symptom_ids}", "{instructions}"):
        assert placeholder not in text
    assert "companion of Lucía" in text
    assert "Answer in Spanish" in text
    assert "Marta may also" in text
    assert ", ".join(SYMPTOMS) in text
    assert text.rstrip().endswith("Prefer short answers.")
    assert '```record\n{"days": [{"date": "YYYY-MM-DD"' in text
    assert "{" + '"days"' in text  # JSON braces survive the filling


# --- extract -------------------------------------------------------------------------------


def test_extract_takes_the_last_block_and_strips_every_block():
    text = (
        'Primero.\n\n```record\n{"days": [], "delete": ["2025-01-01"]}\n```\n'
        "Vale, lo apunto.\n\n```record\n"
        '{"days": [{"date": "2025-01-02", "flow": "light"}], "delete": []}\n```'
    )
    clean, block = assistant.extract(text)
    assert block == {"days": [{"date": "2025-01-02", "flow": "light"}], "delete": []}
    assert clean == "Primero.\n\nVale, lo apunto."
    assert "record" not in clean


def test_extract_without_a_block_or_with_a_broken_one():
    assert assistant.extract("¿Qué tal?") == ("¿Qué tal?", None)
    clean, block = assistant.extract("Hecho.\n```record\n{not json}\n```")
    assert (clean, block) == ("Hecho.", BAD_BLOCK)
    clean, block = assistant.extract('Hecho.\n```record\n{"days": [')
    assert (clean, block) == ("Hecho.", BAD_BLOCK)


def test_extract_takes_a_settings_block_apart_from_the_record_block():
    text = (
        "Vale.\n\n```record\n"
        '{"days": [{"date": "2025-01-02", "flow": "light"}], "delete": []}\n```\n'
        '```settings\n{"ask_at": "21:30"}\n```'
    )
    clean, record = assistant.extract(text)
    assert record == {"days": [{"date": "2025-01-02", "flow": "light"}], "delete": []}
    assert assistant.extract(clean, "settings") == ("Vale.", {"ask_at": "21:30"})
    assert assistant.extract(text, "settings")[1] == {"ask_at": "21:30"}
    assert assistant.extract("Vale.\n```settings\n{", "settings") == ("Vale.", BAD_BLOCK)
    assert assistant.extract("Vale.", "settings") == ("Vale.", None)


# --- context.md ----------------------------------------------------------------------------


def test_context_with_the_fictional_log():
    log = records.load(FIXTURES / "log_fictional.jsonl")
    now = datetime(2025, 9, 1, 21, 14, tzinfo=MADRID)
    text = assistant.context(
        log,
        now,
        tz="Europe/Madrid",
        t=Translator("es"),
        name="Lucía",
        admins=["Marta"],
        birth_date=date(1990, 9, 2),
        ask_at="21:00",
        asked=True,
    )
    assert "Monday (lunes) 2025-09-01, 21:14 Europe/Madrid. Bot day 2025-09-01" in text
    assert "She is 34." in text
    assert "Period day 5 (started Thursday 2025-08-28)." in text
    assert "The bot asked at 21:00 about today; answered." in text
    assert "# Prediction (last 6 cycles)" in text
    assert "After this period: next start around" in text
    assert "lun 2025-09-01 (today): light, pain 0 [day 5]" in text
    assert (
        "jue 2025-08-28: medium, pain 6, abdominal_cramps, lower_back_pain, "
        'note: "me ha pillado en clase de yoga" [day 1]'
    ) in text
    assert "2025-09-02" not in text.split("# Calendar")[1]  # tomorrow is not on it
    assert "abdominal_cramps in " in text
    assert "Lucía (whose cycle this is); Marta (admin)" in text


def test_context_after_midnight_still_talks_about_the_evening_before():
    log = records.load(FIXTURES / "log_fictional.jsonl")
    now = datetime(2025, 9, 2, 0, 30, tzinfo=MADRID)
    text = assistant.context(log, now, tz="Europe/Madrid", t=Translator("es"), name="Lucía")
    assert "Tuesday (martes) 2025-09-02, 00:30 Europe/Madrid. Bot day 2025-09-01" in text
    assert "lun 2025-09-01 (today)" in text
    assert "The bot has not asked today" in text


def test_context_with_no_history():
    text = assistant.context(
        {},
        datetime(2025, 9, 1, 12, tzinfo=MADRID),
        tz="Europe/Madrid",
        t=Translator("es"),
        name="L",
    )
    assert "No cycle start in the log yet." in text
    assert "None yet: the log has no cycle start." in text
    assert "None logged yet." in text


def test_rules_say_a_decline_button_is_no_never_nada():
    # Sergio, 2026-10-14: a tap is an action, so «Nada» never labels a button; the rule is the bot's own knowledge
    text = assistant.rules("Lucía", "es", ["Marta"], "")
    assert "never «Nada»" in text and "Sí / No" in text
