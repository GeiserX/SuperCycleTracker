<p align="center">
  <img src="docs/images/banner.svg" alt="SuperCycleTracker" width="100%">
</p>

<h1 align="center">SuperCycleTracker</h1>

<p align="center">
  <a href="https://github.com/GeiserX/SuperCycleTracker/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/SuperCycleTracker/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GeiserX/SuperCycleTracker?style=flat-square" alt="License"></a>
</p>

SuperCycleTracker is a menstrual cycle tracker that lives in a Telegram group. It asks on the days a period is likely, logs flow, pain and symptoms from buttons or a plain reply, learns each person's cycle to predict the next one, and keeps every record in a private git repo you own. Built for one household; the bot talks Spanish to its users while the code and docs are English.

## Features

- Asks in the evening on the days her period is likely, based on her past cycles. One tap answers.
- Checks in daily while the period lasts, up to day 7 (her setting) or until she says it stopped, and asks once more for days that slipped by. When a period starts, it also asks how the evening before went.
- Logs flow, pain from 0 to 10, symptoms and a short note, from buttons or from her own words.
- She changes the ask hour from the chat, in words or with `/hora 21:30`; the bot commits the profile change itself.
- Predicts the next period from the logged history, and says so when a period closes.
- Imports an Apple Health export to seed the history.
- In the group it reads replies to the bot, answers to its questions, commands, and messages that mention it when Telegram delivers them; as a group admin it reads all of her messages. Voice notes from her or an admin are always heard, when the plugin can transcribe. It also answers private chats with her or an admin.
- Works without any AI: buttons and `/anotar` write records directly. An optional assistant plugin reads free text and answers questions.
- The assistant reads the shared assistant principles first, in English or Spanish (`supercycletracker/principles/`): twelve rules on whom it answers, buttons, offers, privacy and honest results. The bot's own rules in `rules.md` follow them and only add what is specific to tracking a cycle. The files are copied unchanged from the shared text, and a test pins their hashes.
- A burst of messages gets one answer, and what she writes while it thinks joins that answer. An edited message is taken into account. Choices come as buttons she can tap or ignore, and an action the assistant wants to take waits for Approve or Reject.
- Optionally reads the rest of the group as context, marked as not addressed to it, and keeps a chat log so a fresh conversation starts from the last two weeks of the chat. A remark between people is never taken as an order.
- Offers what it can do instead of doing it unasked: a reminder here or in her calendar, or the predicted phases and next period as calendar entries, with buttons. Without an answer nothing happens, and `/recordatorios` lists and cancels the reminders set.
- Every string is translatable. English and Spanish ship.
- Keeps the records in a private git repository you own, one commit per check-in, with `/deshacer` as undo.

## Quick start

The app runs from a data directory: a checkout of your private data repository.

```
data/
  cycle/profile.yaml   who this is and how the bot talks (you edit it)
  cycle/log.jsonl      the records (the bot writes and commits it)
  plugins/             the optional assistant plugin
  state/               bot plumbing, git-ignored
```

Create a bot with BotFather, add it to the group, then:

```sh
uv sync
uv run sct check --data ./data                 # profile and every log line
uv run sct predict --data ./data               # the prediction for today
uv run sct import-apple-health export.zip --data ./data --dry-run
TELEGRAM_BOT_TOKEN=... uv run sct say --data ./data --ask   # a test card in the group
TELEGRAM_BOT_TOKEN=... SCT_GIT_TOKEN=... uv run sct run --data ./data
```

Or with Docker, the data directory mounted at `/data` and owned by uid 1000:

```sh
docker run -d --name supercycletracker \
  -e TELEGRAM_BOT_TOKEN -e SCT_GIT_TOKEN -e TZ=Europe/Madrid \
  -v "$PWD/data:/data" drumsergio/supercycletracker:0.4.3
```

Only one process may poll a bot token. `sct say` does not poll, so it is safe beside the running bot.

| variable | meaning |
|---|---|
| `SCT_DATA` | the data directory (default `/data`) |
| `TELEGRAM_BOT_TOKEN` | the bot token |
| `SCT_GIT_TOKEN` | a token that can push to the data repository, sent as an `Authorization` header |
| `TZ` | the container timezone; dates follow `profile.timezone` |

## Profile

`cycle/profile.yaml`. Unknown keys are ignored.

```yaml
name: Lucía
birth_date: 1993-05-02
timezone: Europe/Madrid
language: es
ask_at: "21:00"             # between 05:00 and 23:59
period_max_day: 7           # optional, 3..14: the last period day asked about daily
telegram:
  chat_id: -100123          # the group
  user_id: 111              # whose cycle this is
  username: lucia_example   # optional: lets the reply box open on her phone by itself
  admins: {222: Marta}      # may use /estado, /nuevo, /presentacion and log for her
  history_scope: asked      # asked | all: all keeps the group's other messages as context
  history_days: 14          # the chat log a fresh assistant conversation starts from
assistant:                  # optional; without it the bot is buttons and commands only
  plugin: plugins/assistant.py
  timeout_s: 600
  model: null
  instructions: ""          # appended to the assistant's rules
calendar:                   # optional; for a plugin that can reach her calendar
  events: false             # offer the predicted phases and next period as calendar entries
  reminders: false          # a reminder can also go in the calendar
  titles: {}                # e.g. {luteal: "Fase lútea", follicular: ""}; "" leaves one out
```

## Commands

| command | who | does |
|---|---|---|
| `/hoy` `[texto]` | her, admins | today's card; with text, the text goes to the assistant |
| `/resumen` | her, admins | last period, recent cycles, usual length, next window |
| `/anotar ayer mucho 6 calambres` | her, admins | note a day without the assistant |
| `/hora 21:30` | her, admins | change the hour of the daily question |
| `/deshacer` | her, admins | undo the last check-in |
| `/recordatorios` | her, admins | the reminders to come, each with a button to cancel it |
| `/ayuda` | her, admins | the list |
| `/estado` | admins | phase, today's question, the assistant, git sync |
| `/nuevo` | admins | start a fresh assistant conversation |
| `/presentacion` | admins | post the introduction |

English aliases work too: `/today`, `/summary`, `/log`, `/time`, `/undo`, `/reminders`, `/help`, `/status`, `/new`, `/about`. `sct say --intro` posts the introduction from the host.

## The assistant plugin

The plugin is one Python file in the data directory, so the app never knows which AI sits behind it.

```python
def converse(turn: dict, settings: dict) -> dict | str: ...  # required
def forget(conversation: str, settings: dict) -> None: ...  # optional, called by /nuevo
# optional, the text of a voice note
def transcribe(audio: bytes, mime: str, language: str, settings: dict) -> str: ...
# optional, a person's answer to an action the assistant asked to take
def answer(request: dict, decision: dict, settings: dict) -> dict: ...
```

`turn` has `conversation` (what the plugin returned last time in this chat, or `None`), `rules`, `context`, `files` (`{"cycle/log.jsonl": <whole file>}`) and `new` (her lines the plugin has not kept yet). `settings` is the `assistant:` block minus `plugin` and `instructions`. Return a string, or `{"text", "conversation", "kept", "later"}`; `later` is an optional callable the app runs on a background thread after posting the text, posting the string it returns. The text may end with one fenced `record` block, and one `settings` block when she asks to change the ask hour. The app validates each, all or nothing, and writes the confirmation itself from what it saved. `kept: false` counts as a failed turn: nothing is applied and her lines go again with the next one. The plugin must return before `timeout_s`.

While it works, the plugin may call `turn["incoming"]()` for the messages that arrived since the turn started and name the ones its answer covered in `covers`; the rest go with the next turn. It may offer choices as buttons (`options`, or an `options` block in the text). A plugin that defines `answer` also gets `turn["ask"]`, which posts an action the assistant wants to take with Approve and Reject and calls `answer` on the tap.

[`tests/fixtures/fake_plugin.py`](tests/fixtures/fake_plugin.py) is a scripted example. The full design is in [`docs/DESIGN.md`](docs/DESIGN.md).

## License

[AGPL-3.0-or-later](LICENSE)
