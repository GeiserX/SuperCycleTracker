"""What a chat answer may do besides its text: react to the message, send a sticker, a photo or a file.

The model asks for them with blocks at the end of its answer; `extract` takes the blocks out of the
text and returns what they ask for, checked and capped, so the text the chat sees never shows them:

    ```telegram
    react: 👍
    sticker: 🎉
    photo: https://example.org/plato.jpg | El plato terminado
    ```

    ````document lista-de-la-compra.md | La lista de esta semana
    - 2 kg de tomates
    ````

- `react`: one emoji of Telegram's reaction set (REACTIONS), put on the message the answer is for. An
  answer that is only a reaction sends no text: that is the "ok" a person would give.
- `sticker`: an emoji; the bot sends the sticker of its own set (its settings) with that emoji, and
  nothing when it has no set or the set has none with it.
- `photo`: an https URL Telegram fetches itself, with an optional caption, only on a host the bot's
  settings list (`photo_hosts`, a host or any of its subdomains). Telegram fetches it with no tap, so
  an open list would let text the model read (a web page, a file) send the chat's data to any server
  in the URL; with no hosts there are no photos. At most PHOTOS_MAX.
- a `document` block (three or more backticks, closed by as many at the start of a line, so a code
  block inside it stays in it): its text sent as a file named as given, for what is long or meant to
  be kept (a list, a recipe, a report). Names lose their accents and are cut to letters, digits, `-`
  and `_` (the caller's `fallback` word when nothing is left), with an extension of DOC_TYPES; at most
  DOCS_MAX, DOC_BYTES each.

Anything else in a block, and anything over a limit, is dropped without a word. The bot sends the text
first and each of these after it on its own, so a reaction or a file Telegram refuses never loses the
answer; a reaction that fails on an answer with no text is sent as that emoji instead. Stdlib only, and
it imports nothing else, so a bot that cannot depend on this package can carry a copy of this file.
"""

import re
import unicodedata

PHOTOS_MAX = 3
DOCS_MAX = 3
DOC_BYTES = 200_000
CAPTION_CHARS = 1000  # under Telegram's 1024 per caption
URL_CHARS = 2000
NAME_CHARS = 64
DOC_TYPES = {
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "ics": "text/calendar",
}

# The emoji a bot may react with (Bot API ReactionTypeEmoji), compared without variation selectors.
REACTIONS = frozenset(
    (
        "👍",
        "👎",
        "❤",
        "🔥",
        "🥰",
        "👏",
        "😁",
        "🤔",
        "🤯",
        "😱",
        "🤬",
        "😢",
        "🎉",
        "🤩",
        "🤮",
        "💩",
        "🙏",
        "👌",
        "🕊",
        "🤡",
        "🥱",
        "🥴",
        "😍",
        "🐳",
        "❤‍🔥",
        "🌚",
        "🌭",
        "💯",
        "🤣",
        "⚡",
        "🍌",
        "🏆",
        "💔",
        "🤨",
        "😐",
        "🍓",
        "🍾",
        "💋",
        "🖕",
        "😈",
        "😴",
        "😭",
        "🤓",
        "👻",
        "👨‍💻",
        "👀",
        "🎃",
        "🙈",
        "😇",
        "😨",
        "🤝",
        "✍",
        "🤗",
        "🫡",
        "🎅",
        "🎄",
        "☃",
        "💅",
        "🤪",
        "🗿",
        "🆒",
        "💘",
        "🙉",
        "🦄",
        "😘",
        "💊",
        "🙊",
        "😎",
        "👾",
        "🤷‍♂",
        "🤷",
        "🤷‍♀",
        "😡",
    )
)

_BLOCK = re.compile(r"\n?^```telegram[ \t]*\r?\n(.*?)^```[ \t]*$\n?", re.MULTILINE | re.DOTALL)
_DOC = re.compile(
    r"\n?^(`{3,})document[ \t]+([^\n]*)\r?\n(.*?)^\1[ \t]*$\n?", re.MULTILINE | re.DOTALL
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def plain(emoji):
    """`emoji` without variation selectors, as Telegram lists its reactions."""
    return emoji.replace("️", "").replace("︎", "").strip()


def _emoji(value):
    """`value` when it is one short run of symbols (no letters, digits or spaces), else None."""
    value = plain(value)
    if not value or len(value) > 8 or re.search(r"[\w\s<>&]", value):
        return None
    return value


def _caption(text):
    text = " ".join(_CONTROL.sub(" ", text).split())
    return text[:CAPTION_CHARS] or None


def _url(value, hosts):
    """`value` when it is a plain https URL on one of `hosts` (or a subdomain of one), else None."""
    value = value.strip()
    host = value.split("/")[2].lower() if value.count("/") >= 2 else ""
    if (
        not value.startswith("https://")
        or len(value) > URL_CHARS
        or re.search(r"[\s<>\"'\\\x00-\x1f\x7f]", value)
        or not re.fullmatch(r"[a-z0-9.-]+", host)
        or not any(host == h or host.endswith("." + h) for h in hosts)
    ):
        return None
    return value


def filename(raw, fallback="file"):
    """A safe file name from what the model wrote («Menú semana.md» is «Menu-semana.md»), `fallback`
    as its stem when no letter or digit is left, or None when no allowed extension is left."""
    name = raw.strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    ext = ext.lower()
    if not dot or ext not in DOC_TYPES:
        return None
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-.")[: NAME_CHARS - len(ext) - 1]
    return f"{stem or fallback}.{ext}"


def _documents(text, fallback):
    docs = []

    def take(m):
        head, body = m.group(2), m.group(3)
        name, _, caption = head.partition("|")
        name = filename(name, fallback)
        data = body.encode("utf-8")
        if name and body.strip() and len(data) <= DOC_BYTES and len(docs) < DOCS_MAX:
            docs.append(
                {
                    "name": name,
                    "data": data,
                    "mime": DOC_TYPES[name.rsplit(".", 1)[1]],
                    "caption": _caption(caption),
                }
            )
        return "\n"

    return _DOC.sub(take, text), docs


def extract(text, photo_hosts=(), fallback="file"):
    """The answer without its blocks, and what they ask for (photos only on `photo_hosts`; a file
    whose name has no letter left is named `fallback`, a word in the chat's language):
    {"react": emoji | None, "sticker": emoji | None, "photos": [{"url", "caption"}],
     "documents": [{"name", "data" (bytes), "mime", "caption"}]}."""
    found = {"react": None, "sticker": None, "photos": [], "documents": []}
    text, found["documents"] = _documents(text.replace("\x00", ""), fallback)
    hosts = [h.strip().lower() for h in photo_hosts if h and h.strip()]
    for block in _BLOCK.findall(text):
        for line in block.splitlines():
            key, sep, value = line.strip().lstrip("-*• ").partition(":")
            key = key.strip().lower()
            if not sep:
                continue
            if key == "react" and found["react"] is None:
                emoji = _emoji(value)
                found["react"] = emoji if emoji in REACTIONS else None
            elif key == "sticker" and found["sticker"] is None:
                found["sticker"] = _emoji(value)
            elif key == "photo" and len(found["photos"]) < PHOTOS_MAX:
                url, _, caption = value.partition(" | ")
                url = _url(url, hosts)
                if url and all(p["url"] != url for p in found["photos"]):
                    found["photos"].append({"url": url, "caption": _caption(caption)})
    text = _BLOCK.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), found


def sticker_for(sticker_set, emoji):
    """The file_id of the first sticker of a getStickerSet result whose emoji is `emoji`, or None."""
    want = plain(emoji or "")
    for s in (sticker_set or {}).get("stickers") or []:
        if want and plain(s.get("emoji") or "") == want and isinstance(s.get("file_id"), str):
            return s["file_id"]
    return None


GUIDE = {
    "en": """## How your answers look in Telegram

Write light Markdown; the bot turns it into Telegram formatting. Short summary first, then the detail.
Never a wall of text: short paragraphs, a blank line between them, lists for anything listed.
- **bold** for what matters most, _italic_, ++underline++, ~~struck out~~, ||spoiler|| (hidden until
  tapped), `code`, [text](https://...) links, `# Heading` (shown bold), `- ` lists, `> ` quotes.
- Detail most people skip (the reasoning, the full list, the sources) goes in a fold, which the chat
  shows folded until tapped. No code block or quote inside a fold:
  ```fold
  the detail
  ```
- Code, a diff or a command goes in a fenced block with its language: ```python ... ```.
- What is long or meant to be kept (a shopping list, a full recipe, a report) goes as a file, with a
  one-line summary in the answer. Use four backticks, a name ending in .md, .txt, .csv, .json or .ics,
  and an optional caption after `|`:
  ````document weekly-list.md | This week's list
  the file's content
  ````
- To act like a person in the chat, end the answer with a block (each line optional):
  ```telegram
  react: 👍
  {extras}```
  `react` puts one emoji on the message you answer: 👍 👌 ❤ 🔥 🎉 👏 🙏 🤝 😁 🤔 👀 🫡 and the rest of
  Telegram's reaction set. When a reaction says it all (a thank-you, an "ok", a fact you just
  noted), answer with only the block and no text.
- Emojis in the text: few, and only where they add something.
{voice}""",
    "es": """## Cómo se ven tus respuestas en Telegram

Escribe Markdown ligero; el bot lo convierte al formato de Telegram. Primero un resumen corto, luego
el detalle. Nunca un muro de texto: párrafos cortos, una línea en blanco entre ellos, listas para lo
que sea una lista.
- **negrita** para lo más importante, _cursiva_, ++subrayado++, ~~tachado~~, ||spoiler|| (oculto hasta
  que lo tocan), `código`, enlaces [texto](https://...), `# Título` (sale en negrita), listas con `- `,
  citas con `> `.
- El detalle que casi nadie lee (el razonamiento, la lista completa, las fuentes) va plegado: el chat
  lo enseña cerrado hasta que lo tocan. Dentro, ni bloques de código ni citas:
  ```fold
  el detalle
  ```
- El código, un diff o un comando, en un bloque con su lenguaje: ```python ... ```.
- Lo largo o lo que se va a guardar (una lista de la compra, una receta entera, un informe) va como
  archivo, con una línea de resumen en la respuesta. Cuatro comillas invertidas, un nombre acabado en
  .md, .txt, .csv, .json o .ics y, si quieres, un pie después de `|`:
  ````document lista-semanal.md | La lista de esta semana
  el contenido del archivo
  ````
- Para comportarte como una persona en el chat, termina la respuesta con un bloque (cada línea es
  opcional):
  ```telegram
  react: 👍
  {extras}```
  `react` pone un emoji en el mensaje al que respondes: 👍 👌 ❤ 🔥 🎉 👏 🙏 🤝 😁 🤔 👀 🫡 y el resto de
  reacciones de Telegram. Cuando una reacción lo dice todo (un gracias, un «vale», un dato que acabas
  de apuntar), responde solo con el bloque, sin texto.
- Emojis en el texto: pocos, y solo donde aportan algo.
{voice}""",
}
_STICKER = {
    "en": "sticker: 🎉   (a sticker of the bot's set with that emoji)\n  ",
    "es": "sticker: 🎉   (un sticker del bot con ese emoji)\n  ",
}
_PHOTO = {
    "en": "photo: https://... | caption   (an image by its address, only from {hosts})\n  ",
    "es": "photo: https://... | pie   (una imagen por su dirección, solo de {hosts})\n  ",
}
_VOICE = {"en": "\n### Your voice in this chat\n\n", "es": "\n### Tu voz en este chat\n\n"}


def guide(lang="en", voice="", stickers=False, photo_hosts=()):
    """The model's instructions for all of the above, in `lang` (English when it has none): the sticker
    and photo lines only when the bot can send them, and the bot's own `voice` (its settings) last."""
    lang = lang if lang in GUIDE else "en"
    hosts = ", ".join(h.strip() for h in photo_hosts if h and h.strip())
    photo = _PHOTO[lang].replace("{hosts}", hosts) if hosts else ""
    extras = (_STICKER[lang] if stickers else "") + photo
    tail = _VOICE[lang] + voice.strip() + "\n" if voice and voice.strip() else ""
    return GUIDE[lang].replace("{extras}", extras).replace("{voice}", tail).strip() + "\n"
