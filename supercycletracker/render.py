"""The model's light Markdown as Telegram HTML, in chunks Telegram accepts.

Everything is escaped first; only these become tags, so nothing the model writes can open a tag of its
own, and markup that would cross another tag stays as written:

- `**x**` and `__x__` bold, `*x*` and `_x_` italic, `++x++` underline, `~~x~~` strike, `||x||` spoiler,
  `` `x` `` code, `[text](https://...)` links (one level of parentheses in the URL);
- `#` headings (bold), `-`/`*`/`+` bullets (•), and `>` lines (a quote);
- a fenced block: with a language (```python) `<pre><code class="language-python">`, without one `<pre>`;
- a ```fold block: an expandable quote, folded to its first lines until it is tapped, for the detail
  most readers skip. Inside it the same inline markup, bullets and headings; no code block, no quote.

`brief` keeps an answer to a few lines: past BRIEF characters, the rest goes in a fold.

NUL characters are removed. Telegram's own parser is the last word: a chunk it still refuses is sent
again as plain text by the caller, so an answer is never lost to its formatting. Stdlib only, and it
imports nothing else, so a bot that cannot depend on this package can carry a copy of this file.
"""

import html
import re

LIMIT = 3500  # under Telegram's 4096 characters per message, with room for the markup
BRIEF = 700  # what an answer shows before the rest is folded: a few lines on a phone

_TAGS = r"b|i|u|s|tg-spoiler"
_BODY = (
    rf"((?:[^<\n]|<({_TAGS})>[^<\n]*</\2>)+?)"  # one line; may hold whole tags, never half of one
)
# The info string (a language) counts only when a newline follows it: inline ```ls -la``` keeps its text.
_FENCE = re.compile(r"```(?:([^\n`]*)\n)?(.*?)```", re.DOTALL)
_LANG = re.compile(r"[A-Za-z0-9_+#.-]{1,24}")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://(?:[^()\s\x00]|\([^()\s\x00]*\))+)\)")
_QUOTE = re.compile(r"^&gt;(?: (.*)|)$")  # "> x" or a bare ">", never ">5 kg"


def _inline(s):
    """`s` is already escaped, so every "<" in it is a tag added here."""
    s = re.sub(r"\*\*(?=\S)" + _BODY + r"(?<=\S)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\w)__(?=\S)" + _BODY + r"(?<=\S)__(?!\w)", r"<b>\1</b>", s)
    s = re.sub(r"\+\+(?=\S)" + _BODY + r"(?<=\S)\+\+", r"<u>\1</u>", s)
    s = re.sub(r"~~(?=\S)" + _BODY + r"(?<=\S)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(?=\S)" + _BODY + r"(?<=\S)\|\|", r"<tg-spoiler>\1</tg-spoiler>", s)
    s = re.sub(r"(?<![\w*])\*(?=[^\s*])" + _BODY + r"(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
    return re.sub(r"(?<!\w)_(?=[^\s_])" + _BODY + r"(?<=[^\s_])_(?!\w)", r"<i>\1</i>", s)


def _quotes(s):
    """Runs of `>` lines (already escaped to `&gt;`) as one quote each."""
    lines, out, run = s.split("\n"), [], []
    for line in [*lines, None]:
        m = _QUOTE.match(line) if line is not None else None
        if m:
            run.append(m.group(1) or "")
            continue
        if run:
            body = "\n".join(run).strip("\n")
            out.append(f"<blockquote>{body}</blockquote>" if body.strip() else "")
            run = []
        if line is not None:
            out.append(line)
    return "\n".join(out)


def _render(md, stash, inner=False):
    s = _CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1), quote=False)}</code>"), md)
    s = _LINK.sub(
        lambda m: stash(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{_inline(html.escape(m.group(1), quote=False))}</a>"
        ),
        s,
    )
    s = html.escape(s, quote=False)
    s = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)[ \t#]*$", r"<b>\1</b>", s)
    s = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", r"\1• ", s)
    s = _inline(s)
    return s if inner else _quotes(s)


def to_html(md):
    """`md` (light Markdown) as Telegram HTML."""
    kept = []

    def stash(markup):
        kept.append(markup)
        return f"\x00{len(kept) - 1}\x00"

    def fence(m):
        info, body = (m.group(1) or "").strip(), m.group(2).strip("\n")
        if info.lower() == "fold":
            # A quote holds no other quote: its own `>` lines stay as text.
            inner = _render(body, stash, inner=True).strip()
            return stash(f"<blockquote expandable>{inner}</blockquote>") if inner else ""
        code = html.escape(body, quote=False)
        if not code.strip():
            return ""
        if _LANG.fullmatch(info):
            return stash(f'<pre><code class="language-{info}">{code}</code></pre>')
        return stash(f"<pre>{code}</pre>")

    s = _FENCE.sub(fence, md.replace("\x00", ""))
    s = _render(s, stash)
    while "\x00" in s:
        s = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], s)
    return s.strip()


def _open_fence(text):
    """The fence still open at the end of `text`, as the line that reopens it: "```fold" or "```python"
    when it opened as one, else a bare "```" (never a long opening line, so a cut always moves on);
    None when every fence is closed."""
    opened = None
    for m in re.finditer(r"```([^\n`]*)", text):
        info = m.group(1).strip()
        keep = info.lower() == "fold" or _LANG.fullmatch(info)
        opened = None if opened is not None else "```" + (info if keep else "")
    return opened


def brief(md, limit=BRIEF):
    """`md` as a few lines: past `limit` characters, the rest goes in a ```fold block, cut at a blank
    line (else a line end), so a long answer reads short and its detail is one tap away. Left as
    written when it is short, when it holds a fenced block, when a `>` quote comes after the cut (a
    fold holds neither), or when no line end falls within `limit`."""
    text = md.strip()
    if len(text) <= limit or "```" in text:
        return text
    cut = text.rfind("\n\n", 0, limit)  # the last paragraph that fits, however short the head
    if cut <= 0:
        cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        return text
    head, rest = text[:cut].rstrip(), text[cut:].strip()
    if not rest or re.search(r"(?m)^>", rest):
        return text
    return f"{head}\n```fold\n{rest}\n```"


def split(text, limit=LIMIT):
    """`text` in chunks of at most `limit` characters (plus a repaired fence), cut at a blank line,
    else a line end, else a space, so a chunk rarely splits a paragraph and never a word unless one
    is longer than `limit`. A cut inside a fenced block closes the fence in the first chunk and
    reopens it in the next with its own opening line, so both halves render as the same kind of block
    (code in its language, or a fold)."""
    text = text.replace("\x00", "").strip()
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        head, text = text[:cut].rstrip(), text[cut:].strip()
        opened = _open_fence(head)
        if opened is not None:
            head += "\n```"
            text = opened + "\n" + text
        chunks.append(head)
    if text:
        chunks.append(text)
    return chunks
