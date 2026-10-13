"""A scripted assistant plugin: the only example plugin this repository ships.

The app loads it from the data directory like any plugin. A test reaches the loaded module through
`assistant.load_module` and scripts it:

- `REPLIES`: what the next calls return, in order. An item is a string, a dict
  (`{"text", "conversation", "kept"}`), an exception (raised), or a callable that gets the turn and
  returns one of those. With the list empty the plugin answers «Vale.» and keeps the turn.
- `DOWN`: while true every call raises, as a provider that is down.
- `CALLS`: every turn and settings received; `FORGOTTEN`: every conversation dropped.
"""

import copy

REPLIES: list = []
CALLS: list = []
FORGOTTEN: list = []
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
