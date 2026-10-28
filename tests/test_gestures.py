"""How an answer looks and acts in Telegram: the renderer, the gestures block (including hostile
model output) and the bot sending them after the text."""

import re

from conftest import GROUP, PROFILE
from test_bot import QUESTION, plugin, say

from supercycletracker import gestures, render
from supercycletracker.bot import Profile

TAGS = r"b|i|u|s|tg-spoiler|code|pre|blockquote|a"
WITH_STICKERS = PROFILE.replace(
    "  admins: {222: Marta}\n",
    "  admins: {222: Marta}\n  style: Cálida y discreta.\n  sticker_set: flores\n",
)


def sent(bot):
    return bot.tg.of("sendMessage")


def test_render_folds_quotes_and_code():
    out = render.to_html("**Hoy**\n> cita\n```fold\n- a ||b||\n```\n```json\n{}\n```")
    assert out == (
        "<b>Hoy</b>\n<blockquote>cita</blockquote>\n<blockquote expandable>• a "
        '<tg-spoiler>b</tg-spoiler></blockquote>\n<pre><code class="language-json">{}</code></pre>'
    )


def test_render_hostile_markup_never_opens_a_tag():
    for md in (
        "<blockquote expandable>x",
        "||<b>x</b>||",
        "```fold\n</blockquote><b>\n```",
        '```py" onclick="x\ny\n```',
        '[x](https://a.org/"><script>)',
    ):
        out = render.to_html(md)
        assert sorted(re.findall(rf"<({TAGS})[ >]", out)) == sorted(
            re.findall(rf"</({TAGS})>", out)
        ), (md, out)
        assert "<script" not in out and "onclick" not in out


def test_gestures_hostile_output_is_dropped():
    text, g = gestures.extract(
        "x\n```telegram\nreact: <b>\nsticker: abc\nphoto: http://a.org/a.jpg\n"
        "photo: https://u:p@evil.org/a.jpg\nsend_message: -100 hola\n```\n"
        "```document run.sh\necho\n```\n"
    )
    assert text == "x"
    assert g == {"react": None, "sticker": None, "photos": [], "documents": []}


def test_answer_goes_as_html_and_a_refused_markup_as_plain_text(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["**Anotado**, gracias."]
    say(bot, "hoy poco", reply_to=QUESTION)
    assert (sent(bot)[-1]["text"], sent(bot)[-1]["parse_mode"]) == (
        "<b>Anotado</b>, gracias.",
        "HTML",
    )
    bot.tg.fail_next("sendMessage", 400, "Bad Request: can't parse entities")
    plugin(bot).REPLIES[:] = ["**Otra** vez."]
    say(bot, "¿y?", reply_to=QUESTION)
    assert sent(bot)[-1]["text"] == "**Otra** vez." and "parse_mode" not in sent(bot)[-1]


def test_a_reaction_alone_says_nothing_and_a_failed_one_says_the_emoji(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["```telegram\nreact: ❤️\n```"]
    before = len(sent(bot))
    msg = say(bot, "gracias", reply_to=QUESTION)
    [r] = bot.tg.of("setMessageReaction")
    assert (r["chat_id"], r["message_id"]) == (GROUP, msg["message_id"])
    assert r["reaction"] == [{"type": "emoji", "emoji": "❤"}]
    assert len(sent(bot)) == before
    bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 🙏\n```"]
    say(bot, "gracias otra vez", reply_to=QUESTION)
    assert sent(bot)[-1]["text"] == "🙏"


def test_a_summary_goes_as_a_file_and_a_refused_file_never_loses_the_text(make_bot):
    bot = make_bot()
    bot.tg.fail_next("sendDocument", 400, "Bad Request: file is empty")
    plugin(bot).REPLIES[:] = ["Te lo paso.\n````document resumen.md\n# Ciclo\n````"]
    say(bot, "resumen", reply_to=QUESTION)
    assert sent(bot)[-1]["text"] == "Te lo paso."
    plugin(bot).REPLIES[:] = ["Ahí va.\n````document resumen.md | Marzo\n# Ciclo\n````"]
    say(bot, "otra vez", reply_to=QUESTION)
    doc = bot.tg.of("sendDocument")[-1]
    assert (doc["document"], doc["data"], doc["caption"]) == ("resumen.md", b"# Ciclo\n", "Marzo")


def test_a_sticker_needs_the_profiles_set_and_the_voice_reaches_the_rules(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["Hecho.\n```telegram\nsticker: 🌸\n```"]
    say(bot, "hola", reply_to=QUESTION)
    assert bot.tg.of("sendSticker") == []
    assert "sticker:" not in plugin(bot).CALLS[-1]["turn"]["rules"]
    bot = make_bot(profile=WITH_STICKERS)
    plugin(bot).REPLIES[:] = ["Hecho.\n```telegram\nsticker: 🌸\n```"]
    say(bot, "hola", reply_to=QUESTION)
    assert bot.tg.of("sendSticker")[-1]["sticker"] == "STK"
    rules = plugin(bot).CALLS[-1]["turn"]["rules"]
    assert "sticker: 🎉" in rules and rules.rstrip().endswith("Cálida y discreta.")


def test_the_voice_and_switches_come_from_the_profile():
    p = Profile.from_dict({"name": "x", "telegram": {"style": 3, "photo_hosts": "a.org"}})
    assert (p.style, p.sticker_set, p.photo_hosts) == ("", "", ())
    p = Profile.from_dict({"name": "x", "telegram": {"photo_hosts": ["a.org", 3, ""]}})
    assert p.photo_hosts == ("a.org",)


def test_a_file_with_no_latin_letter_is_named_in_the_chats_language(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["Toma.\n```document 日本.txt\nx\n```"]
    say(bot, "hola", reply_to=QUESTION)
    assert bot.tg.of("sendDocument")[0]["document"] == "archivo.txt"
