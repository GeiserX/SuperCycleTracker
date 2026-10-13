"""The service log holds event kinds and counts only.

A full scripted flow runs with the log captured at DEBUG: a scheduled card walked with taps, replies
the assistant answers with a record block, a block refused for a symptom that carries a planted
word, a plugin that fails with her words in its exception, /anotar, /resumen, /estado, /deshacer
and a git sync. None of her words, the assistant's words, the dates, the values or the names may
appear in the log. The same flow with one planted `log.info(text)` must go red.
"""

import json
import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import Clock, FakeTelegram

from supercycletracker import assistant
from supercycletracker import bot as bot_module
from supercycletracker.bot import Bot
from supercycletracker.records import FLOWS, SYMPTOMS

FIXTURES = Path(__file__).parent / "fixtures"
GROUP, HER, ADMIN, BOT_ID = -100123, 111, 222, 999
DAY = date(2027, 3, 27)  # inside the fictional seed's window
PLANTED = "berenjenaplantada"

PROFILE = """\
name: Lucía
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
"""

HER_WORDS = [
    "me duele muchísimo la tripa desde anoche",
    "ayer manché un poquito al salir del gimnasio",
    "¿es normal tener tantos coágulos?",
]
BOT_WORDS = [
    "Vaya, siento que te duela tanto",
    "Eso que cuentas pasa a menudo",
    "nota escrita con sus palabras",
]
SECRETS = [
    *HER_WORDS,
    *BOT_WORDS,
    PLANTED,
    "Lucía",
    "Marta",
    "lucia_example",
    "2027-03-2",  # every date of the flow, iso
    "2027032",  # and in callback data
    "de marzo",  # and in Spanish
    *SYMPTOMS,
    *(f for f in FLOWS if f != "none"),
    "poquito",
    "hinchazón",
]


def block(*days):
    return "\n\n```record\n" + json.dumps({"days": list(days), "delete": []}) + "\n```"


def reply(bot, text):
    bot.handle(
        {
            "message": {
                "message_id": hash(text) % 10_000,
                "from": {"id": HER},
                "chat": {"id": GROUP, "type": "supergroup"},
                "text": text,
                "reply_to_message": {"from": {"id": BOT_ID}, "text": "pregunta"},
            }
        }
    )
    bot.worker.join_idle()


def command(bot, text, user=HER):
    bot.handle(
        {
            "message": {
                "message_id": 1,
                "from": {"id": user},
                "chat": {"id": GROUP, "type": "supergroup"},
                "text": text,
            }
        }
    )


def tap(bot, data, message_id):
    bot.handle(
        {
            "callback_query": {
                "id": "cb",
                "from": {"id": HER},
                "message": {"message_id": message_id, "chat": {"id": GROUP}},
                "data": data,
            }
        }
    )


def scripted_flow(bot):
    k = DAY.strftime("%Y%m%d")
    bot.tick()  # the scheduled card at 21:00 on a waiting day
    (card,) = bot.tg.of("sendMessage")
    mid = 1001  # FakeTelegram's first message id
    for data in (f"y:{k}:medium", f"p:{k}:9", f"s:{k}:abdominal_cramps", f"d:{k}"):
        tap(bot, data, mid)
    mod = assistant.load_module(bot.data, "plugins/fake.py")
    mod.REPLIES[:] = [
        BOT_WORDS[0]
        + block({"date": "2027-03-26", "symptoms": ["headache"], "note": BOT_WORDS[2]}),
        BOT_WORDS[1] + block({"date": "2027-03-25", "symptoms": [f"dolor_{PLANTED}"]}),
        RuntimeError(f"provider refused: {HER_WORDS[2]}"),
    ]
    for text in HER_WORDS:
        reply(bot, text)
    command(bot, "/anotar ayer poquito 3 hinchazón")
    command(bot, "/resumen")
    command(bot, "/estado", user=ADMIN)
    command(bot, "/deshacer")
    bot.repo.flush(force=True)
    command(bot, "/anotar anteayer mucho")
    bot.repo.flush(force=True)
    bot.tg.fail_next("sendMessage", 400, "Bad Request: chat not found")
    help_update = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "from": {"id": HER},
            "chat": {"id": GROUP, "type": "supergroup"},
            "text": "/ayuda",
        },
    }
    bot.tg.updates = [[help_update]]
    bot.poll_once()  # a send that fails inside an update is logged by the loop
    return card


@pytest.mark.parametrize("planted", [False, True])
def test_the_service_log_holds_no_words_dates_values_or_names(
    data_repo, caplog, monkeypatch, planted
):
    (data_repo.data / "cycle" / "profile.yaml").write_text(PROFILE, encoding="utf-8")
    (data_repo.data / "plugins").mkdir()
    shutil.copy(FIXTURES / "fake_plugin.py", data_repo.data / "plugins" / "fake.py")
    if planted:  # the positive control: one careless line must turn the test red
        original = Bot.queue_text

        def leaky(self, chat, msg, content, role):
            bot_module.log.info("queued %s", content)
            return original(self, chat, msg, content, role)

        monkeypatch.setattr(Bot, "queue_text", leaky)
    caplog.set_level(logging.DEBUG)
    bot = Bot(
        data_repo.data,
        FakeTelegram(),
        now=Clock(datetime(2027, 3, 27, 21, 0, tzinfo=ZoneInfo("Europe/Madrid"))),
    )
    try:
        bot.start()
        scripted_flow(bot)
    finally:
        bot.close()

    texts = bot.tg.texts()
    assert any(t.startswith("Un dolor de 9") for t in texts)  # the flow really ran
    assert any(t.startswith("Anotado:") for t in texts)
    assert any(t.startswith("No he sabido anotarlo bien") for t in texts)
    assert any(t.startswith("Ahora mismo no puedo") for t in texts)
    assert "block refused: unknown_symptom" in caplog.text
    assert "git push ok" in caplog.text

    found = [s for s in SECRETS if s.lower() in caplog.text.lower()]
    if planted:
        assert found, "the planted log line was not caught"
    else:
        assert found == [], found
