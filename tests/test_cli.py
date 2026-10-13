"""`sct check`, `sct predict` and `sct say --ask` on fictional data."""

import contextlib
import io
import shutil
from pathlib import Path

import pytest
from conftest import FakeTelegram

from supercycletracker import cli, telegram

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = """\
name: Lucía
timezone: Europe/Madrid
language: es
ask_at: "{ask_at}"
telegram:
  chat_id: -100123
  user_id: 111
  admins: {{222: Marta}}
"""


def sct(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def data(tmp_path):
    (tmp_path / "cycle").mkdir()
    shutil.copy(FIXTURES / "log_fictional.jsonl", tmp_path / "cycle" / "log.jsonl")
    (tmp_path / "cycle" / "profile.yaml").write_text(
        PROFILE.format(ask_at="21:00"), encoding="utf-8"
    )
    return tmp_path


def test_check_passes_on_the_fictional_log(data):
    code, out, err = sct("check", "--data", data)
    assert (code, err) == (0, "")
    assert out == "profile ok\nlog ok: 53 days, 12 starts\n"


def test_check_reports_bad_lines_by_number_and_code_only(data):
    log = data / "cycle" / "log.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"heavy"', '"torrential"')
    lines[4] = "{not json"
    lines[7] = lines[6]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out, err = sct("check", "--data", data)
    assert code == 1
    assert err == "log line 2: unknown_flow\nlog line 5: bad_json\nlog line 8: duplicate_date\n"
    assert "torrential" not in out + err


def test_check_reports_a_flag_that_differs_from_the_rule_without_failing(data):
    log = data / "cycle" / "log.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"cycle_start": true, ', "")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out, _ = sct("check", "--data", data)
    assert code == 0
    assert "log line 1: cycle_start differs from the 10-day rule" in out


@pytest.mark.parametrize("ask_at", ["04:59", "24:00", "9pm"])
def test_check_refuses_an_ask_at_outside_the_bot_day(data, ask_at):
    (data / "cycle" / "profile.yaml").write_text(PROFILE.format(ask_at=ask_at), encoding="utf-8")
    code, _, err = sct("check", "--data", data)
    assert code == 1
    assert "profile: bad_ask_at" in err


def test_check_accepts_the_edges_of_the_bot_day(data):
    for ask_at in ("05:00", "23:59"):
        (data / "cycle" / "profile.yaml").write_text(
            PROFILE.format(ask_at=ask_at), encoding="utf-8"
        )
        assert sct("check", "--data", data)[0] == 0


@pytest.mark.parametrize(
    ("value", "ok"),
    [("3", True), ("14", True), ("2", False), ("15", False), ("seven", False), ("true", False)],
)
def test_check_holds_period_max_day_to_3_through_14(data, value, ok):
    profile = data / "cycle" / "profile.yaml"
    profile.write_text(profile.read_text() + f"period_max_day: {value}\n", encoding="utf-8")
    code, _, err = sct("check", "--data", data)
    assert (code, err) == ((0, "") if ok else (1, "profile: bad_period_max_day\n"))


def test_check_refuses_a_plugin_outside_the_data_dir(data):
    profile = data / "cycle" / "profile.yaml"
    profile.write_text(profile.read_text() + "assistant:\n  plugin: ../x.py\n", encoding="utf-8")
    code, _, err = sct("check", "--data", data)
    assert (code, err) == (1, "profile: plugin_outside_data\n")


def test_predict_prints_the_prediction(data):
    code, out, _ = sct("predict", "--data", data, "--today", "2025-09-10")
    assert code == 0
    assert "starts: 12" in out
    assert "lengths: 32, 28, 34, 29, 27, 31" in out
    assert "expected: 2025-09-27" in out
    assert "window: 2025-09-24 .. 2025-10-01" in out
    assert "phase: quiet" in out


def test_say_ask_sends_a_test_card_and_leaves_last_ask_alone(data, monkeypatch):
    sent = []

    def fake(token):
        tg = FakeTelegram()
        sent.append(tg)
        return tg

    monkeypatch.setattr(telegram, "Telegram", fake)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    code, _, err = sct("say", "--data", data, "--ask")
    assert (code, err) == (0, "")
    (card,) = sent[0].of("sendMessage")
    assert card["chat_id"] == -100123
    assert card["text"].endswith(" (prueba)")
    assert card["reply_markup"]["inline_keyboard"]
    from supercycletracker.repo import KV

    kv = KV(data / "state" / "sct.sqlite")
    assert kv.get("last_ask") is None
    kv.close()


def test_say_intro_posts_the_introduction_to_the_group(data, monkeypatch):
    sent = []

    def fake(token):
        tg = FakeTelegram()
        sent.append(tg)
        return tg

    monkeypatch.setattr(telegram, "Telegram", fake)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    code, out, err = sct("say", "--data", data, "--intro")
    assert (code, out, err) == (0, "intro sent\n", "")
    (intro,) = sent[0].of("sendMessage")
    assert intro["chat_id"] == -100123
    assert intro["text"].startswith(
        "Hola. Soy el bot que lleva la cuenta del ciclo de Lucía en este grupo.\n"
    )
    assert intro["text"].endswith("/ayuda tiene la lista de lo que entiendo.")
    assert "reply_markup" not in intro
    with pytest.raises(SystemExit):  # one or the other
        sct("say", "--data", data, "--intro", "--ask")


def test_say_without_a_token_or_without_ask_refuses(data, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert sct("say", "--data", data, "--ask")[0] == 2
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    assert sct("say", "--data", data)[0] == 2


def test_run_refuses_without_a_token(data, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert sct("run", "--data", data)[0] == 2


def test_predict_defaults_to_the_bot_day_not_the_calendar_day(data, monkeypatch):
    class Clock(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return cli.datetime(2025, 9, 10, 0, 0, tzinfo=tz)  # 02:00 in Madrid

    monkeypatch.setattr(cli, "datetime", Clock)
    code, out, _ = sct("predict", "--data", data)
    assert code == 0
    assert "today: 2025-09-09" in out


def test_check_refuses_admins_that_are_not_a_mapping(data):
    profile = data / "cycle" / "profile.yaml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("{222: Marta}", "[222]"), encoding="utf-8"
    )
    code, _, err = sct("check", "--data", data)
    assert code == 1
    assert "profile: bad_profile" in err


def test_import_refuses_without_a_profile(data):
    (data / "cycle" / "profile.yaml").unlink()
    code, out, err = sct("import-apple-health", FIXTURES / "export_fictional.xml", "--data", data)
    assert (code, out) == (1, "")
    assert err == "profile: missing_profile\n"


def test_import_refuses_a_bad_timezone(data):
    profile = data / "cycle" / "profile.yaml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("Europe/Madrid", "Mars/Olympus"),
        encoding="utf-8",
    )
    code, out, err = sct("import-apple-health", FIXTURES / "export_fictional.xml", "--data", data)
    assert (code, out) == (1, "")
    assert err == "profile: bad_timezone\n"
