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


def test_say_without_a_token_or_without_ask_refuses(data, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert sct("say", "--data", data, "--ask")[0] == 2
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    assert sct("say", "--data", data)[0] == 2


def test_run_refuses_without_a_token(data, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert sct("run", "--data", data)[0] == 2
