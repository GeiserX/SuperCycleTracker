"""A scripted assistant plugin: the only example plugin this repository ships.

The app loads it from the data directory like any plugin. A test reaches the loaded module through
`assistant.load_module` and scripts it:

- `REPLIES`: what the next calls return, in order. An item is a string, a dict
  (`{"text", "conversation", "kept"}`), an exception (raised), or a callable that gets the turn and
  returns one of those. With the list empty the plugin answers «Vale.» and keeps the turn.
- `DOWN`: while true every call raises, as a provider that is down.
- `CALLS`: every turn and settings received; `FORGOTTEN`: every conversation dropped.
- `VOICES`: what the next `transcribe` calls return, in order (a string, an exception that is
  raised, or a callable that returns one of those). With the list empty `transcribe` raises
  `NotImplementedError`, as a provider that cannot listen. `HEARD`: every `(audio, mime, language)`
  received.
- `ANSWERS`: what the next `answer` calls return (with the list empty, `{"ok": True}`);
  `ANSWERED`: every `(request, decision)` received. A reply callable reaches `turn["incoming"]`
  and `turn["ask"]` through the turn it gets.

A reply dict may carry `later`, a callable with no arguments that returns a string: the app runs
it after posting the text.
"""

import copy

REPLIES: list = []
CALLS: list = []
FORGOTTEN: list = []
VOICES: list = []
HEARD: list = []
ANSWERS: list = []
ANSWERED: list = []
DOWN = False


class Down(Exception):
    pass


def converse(turn, settings):
    CALLS.append({"turn": copy.deepcopy(turn), "settings": dict(settings)})
    if DOWN:
        raise Down("provider down")
    reply = REPLIES.pop(0) if REPLIES else {"text": "Vale.", "conversation": "conv-1", "kept": True}
    if not isinstance(reply, BaseException) and callable(reply):
        reply = reply(turn)
    if isinstance(reply, BaseException):
        raise reply
    return reply


def forget(conversation, settings):
    FORGOTTEN.append(conversation)


def transcribe(audio, mime, language, settings):
    HEARD.append((audio, mime, language))
    if not VOICES:
        raise NotImplementedError
    out = VOICES.pop(0)
    if not isinstance(out, BaseException) and callable(out):
        out = out()
    if isinstance(out, BaseException):
        raise out
    return out


def answer(request, decision, settings):
    ANSWERED.append((dict(request), dict(decision)))
    return ANSWERS.pop(0) if ANSWERS else {"ok": True, "error": None}
