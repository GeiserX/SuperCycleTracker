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
- Checks in daily while the period lasts, up to 7 days or until she says it stopped, and asks once more for days that slipped by.
- Logs flow, pain from 0 to 10, symptoms and a short note, from buttons or from her own words.
- Predicts the next period from the logged history, and says so when a period closes.
- Imports an Apple Health export to seed the history.
- In the group it reads replies to the bot, answers to its questions, commands, and messages that mention it when Telegram delivers them. It also answers private chats with her or an admin.
- Works without any AI: buttons and `/anotar` write records directly. An optional assistant plugin reads free text and answers questions.
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
  -v "$PWD/data:/data" drumsergio/supercycletracker:0.1.0
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
telegram:
  chat_id: -100123          # the group
  user_id: 111              # whose cycle this is
  username: lucia_example   # optional: lets the reply box open on her phone by itself
  admins: {222: Marta}      # may use /estado, /nuevo, /presentacion and log for her
assistant:                  # optional; without it the bot is buttons and commands only
  plugin: plugins/assistant.py
  timeout_s: 600
  model: null
  instructions: ""          # appended to the assistant's rules
```

## Commands

| command | who | does |
|---|---|---|
| `/hoy` `[texto]` | her, admins | today's card; with text, the text goes to the assistant |
| `/resumen` | her, admins | last period, recent cycles, usual length, next window |
| `/anotar ayer mucho 6 calambres` | her, admins | note a day without the assistant |
| `/deshacer` | her, admins | undo the last check-in |
| `/ayuda` | her, admins | the list |
| `/estado` | admins | phase, today's question, the assistant, git sync |
| `/nuevo` | admins | start a fresh assistant conversation |
| `/presentacion` | admins | post the introduction |

English aliases work too: `/today`, `/summary`, `/log`, `/undo`, `/help`, `/status`, `/new`, `/about`.

## The assistant plugin

The plugin is one Python file in the data directory, so the app never knows which AI sits behind it.

```python
def converse(turn: dict, settings: dict) -> dict | str: ...  # required
def forget(conversation: str, settings: dict) -> None: ...  # optional, called by /nuevo
```

`turn` has `conversation` (what the plugin returned last time in this chat, or `None`), `rules`, `context`, `files` (`{"cycle/log.jsonl": <whole file>}`) and `new` (her lines the plugin has not kept yet). `settings` is the `assistant:` block minus `plugin` and `instructions`. Return a string, or `{"text", "conversation", "kept"}`. The text may end with one fenced `record` block. The app validates it, all or nothing, and writes the confirmation itself from what it saved. `kept: false` counts as a failed turn: nothing is applied and her lines go again with the next one. The plugin must return before `timeout_s`.

[`tests/fixtures/fake_plugin.py`](tests/fixtures/fake_plugin.py) is a scripted example. The full design is in [`docs/DESIGN.md`](docs/DESIGN.md).

## License

[AGPL-3.0-or-later](LICENSE)
