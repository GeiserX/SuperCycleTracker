"""The bot: the cards with their buttons, free text for the assistant, and the worker thread.

A tap is recorded at once, with no AI: each button carries its date and value, and the next step is
computed from the log, never from a session table, so a stale keyboard still works and a restart
mid-card loses nothing. Text meant for the bot is queued in SQLite (`pending:<chat>`) and one worker
thread runs assistant turns, one at a time, so a tap is answered while the assistant thinks. Telegram's
offset moves only once a line is written, and a line leaves the queue only once its answer is sent,
so a restart answers what was queued or in flight (`resume`). A voice
note from her or an admin is queued the same way and turned into text by the plugin on a hearing
thread. A photo or any other file from her or an admin is queued as a line naming it, and the turn
that takes it downloads it and hands it to the plugin (`turn["attachments"]`). Slow work a plugin hands back with its answer (`later`) runs on a third thread, one job at a
time, and a person's answer to an action the assistant wants to take goes back on a fourth.

One answer per burst: a turn starts DEBOUNCE_S after the chat's newest message, and waits up to
VOICE_WAIT_S for a voice note of that chat still being heard. What arrives while the turn runs is
handed to the plugin (`turn["incoming"]`); the answer names what it covered, and the rest goes with
the next turn. An answer that covers one message replies to it; one that covers several is sent
plainly. An edited message is handled by where it is: still queued, its new wording goes; in the
running turn, a correction joins it; already answered, a new turn checks that answer; an edit that
now addresses the bot is a new message.

The service log gets event kinds and counts only: never a message text, a date of a record or a
value.
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from supercycletracker import assistant, cycle, records
from supercycletracker.i18n import Translator
from supercycletracker.records import SYMPTOMS, BlockError, Change, Day, Edit, Log
from supercycletracker.repo import DATA_LOCK, KV, STATE_DB, GitError, Repo
from supercycletracker.telegram import TelegramError, safe

log = logging.getLogger("sct.bot")

PROFILE_FILE = "cycle/profile.yaml"
PENDING_MAX = 10  # the newest lines kept for the assistant
PENDING_MAX_AGE_S = 7 * 24 * 3600  # and none older than this
PICK_DAYS = 6  # «Sí, otro día» offers the six days before the card's date
SYMPTOM_BUTTONS = 6
DEFAULT_SYMPTOMS = (
    "abdominal_cramps",
    "bloating",
    "headache",
    "diarrhea",
    "constipation",
    "lower_back_pain",
)
# the eve step's toggles (the day before a start): her most frequent there, padded with these
DEFAULT_EVE_SYMPTOMS = (
    "mood_changes",
    "fatigue",
    "abdominal_cramps",
    "bloating",
    "headache",
    "lower_back_pain",
)
EVE_SUMMARY_MIN = 3  # /resumen tells the day-before pattern from this many starts with one
PAIN_HIGH = 8  # a day crossing into this pain or more posts `pain_high`
TEXT_MAX = 4096  # Telegram's limit on one message, in UTF-16 code units
VOICE_MAX = 20 * 1024 * 1024  # the largest file the Bot API lets a bot download
FILE_MAX = VOICE_MAX  # a photo or a file, the same limit
TURN_FILES_MAX = 25 * 1024 * 1024  # the photos and files one turn carries at most
ECHO_MAX = 4000  # the «🎤» echo of a transcript is cut to this, in UTF-16 code units
ADMIN_STATUSES = frozenset({"administrator", "creator"})  # a group admin gets every message
NOT_MODIFIED = "message is not modified"
HER, ADMIN = "her", "admin"
ASK_WINDOW = timedelta(hours=2)  # a bot that was down at ask_at still asks up to 2 h later
ASK_EARLIEST = clock_time(5, 0)  # a bot day starts at 05:00, so ask_at must not be earlier
PERIOD_MAX_DAYS = range(3, 15)  # the values `period_max_day` may take
POLL_S = 20  # getUpdates long poll
WAIT_409_S = 30  # another poller holds the token: wait and keep the offset
TICK_S = 60  # a long wait ticks at least this often: the healthcheck allows 180 s
BACKOFF_MIN_S, BACKOFF_MAX_S = 5, 300  # 5xx and network failures
MENU = ("hoy", "resumen", "anotar", "hora", "deshacer", "recordatorios", "ayuda")  # the menu
DEBOUNCE_S = 2.5  # a chat's turn starts this long after its newest message: a burst goes together
VOICE_WAIT_S = 30  # and waits this long at most, from a note's arrival, for a note being heard
SAID_MAX = 30  # answered messages kept per chat, so an edit of one can be compared with it
ANSWER_CLIP = 1500  # characters of the earlier answer that an edit of an answered message carries
OPTIONS_TTL_S = 12 * 3600  # a choice under an answer can be tapped this long
OPTIONS_KEEP = 20  # offers kept per chat
ASK_DEFAULT_S = 300  # an ask that names no end of its own waits this long
NO_ONE = "no one could be asked"  # the note of an ask declined because nobody could answer it
SETTINGS_KEYS = ("ask_at", "period_max_day")  # what a `settings` block or /hora may change
HISTORY_SCOPES = ("asked", "all")  # telegram.history_scope: which group messages are logged
HISTORY_DAYS = 14  # telegram.history_days by default: the recap a fresh conversation starts from
HISTORY_DAYS_MAX = 60
CONTEXT_MAX = 50  # unaddressed group lines a turn takes as context, the newest
LOG_KEEP_DAYS = 7  # the chat log keeps at least this much, for the context lines of a turn
NOT_TO_YOU = "[not to you] "  # the mark of a group line that did not call the bot
OFFER_GONE = "remind_timeout"  # the label an offer's buttons give way to when nobody answered
REMIND_TTL_S = 1800  # an offered reminder can be tapped this long; then nothing is done
REMIND_DAYS = 60  # the furthest ahead a reminder may be
LATE_S = 600  # a reminder posted this much after its time says so
PLAN_TTL_S = 12 * 3600  # the calendar plan's offer can be tapped this long
PLAN_HOURS = (10, 22)  # the plan is offered between these local hours only
GRANT_SLACK_S = 60  # a tap approves an addition at its time, give or take this

_COMMAND = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$", re.DOTALL)
_CALLBACK_DATE = re.compile(r"^\d{8}$")
_VALUES = {
    "y": frozenset({"unspecified", "light", "medium", "heavy"}),
    "f": frozenset({"none", "light", "medium", "heavy", "same"}),
    "p": frozenset(str(n) for n in range(11)),
    "s": frozenset(SYMPTOMS),
    "e": frozenset(SYMPTOMS),
}
_NO_VALUE = frozenset({"d", "ed", "pick", "back", "wait", "skip", "fix", "more"})


# --- the profile ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """The keys of `cycle/profile.yaml` the app reads; unknown keys are ignored."""

    name: str
    timezone: str = "UTC"
    language: str = "en"
    ask_at: str = "21:00"
    period_max_day: int | None = cycle.PERIOD_MAX_DAY  # None: not an integer (profile_errors)
    birth_date: date | None = None
    chat_id: int | None = None
    user_id: int | None = None
    username: str | None = None
    admins: Mapping[int, str] = field(default_factory=dict)
    history_scope: str = "asked"
    history_days: int | None = HISTORY_DAYS  # None: not a whole number (profile_errors)
    assistant: Mapping | None = None
    calendar: Mapping = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> Profile:
        if not isinstance(raw, Mapping):
            raise ValueError("bad_profile")  # noqa: TRY004 - bad data, one exception type
        tg = raw.get("telegram") or {}
        if not isinstance(tg, Mapping):
            raise ValueError("bad_profile")  # noqa: TRY004
        birth = raw.get("birth_date")
        if isinstance(birth, str):
            birth = date.fromisoformat(birth)
        admins = tg.get("admins") or {}
        if not isinstance(admins, Mapping):
            raise ValueError("bad_profile")  # noqa: TRY004
        username = tg.get("username")
        block = raw.get("assistant")
        max_day = raw.get("period_max_day")
        days = tg.get("history_days")
        calendar = raw.get("calendar")
        return cls(
            name=str(raw.get("name") or ""),
            timezone=str(raw.get("timezone") or "UTC"),
            language=str(raw.get("language") or "en"),
            ask_at=_clock_text(raw.get("ask_at")),
            period_max_day=cycle.PERIOD_MAX_DAY if max_day is None else _int(max_day),
            birth_date=birth if isinstance(birth, date) else None,
            chat_id=_int(tg.get("chat_id")),
            user_id=_int(tg.get("user_id")),
            username=str(username).lstrip("@") if username else None,
            admins={int(k): str(v) for k, v in admins.items()},
            history_scope=str(tg.get("history_scope") or "asked"),
            history_days=HISTORY_DAYS if days is None else _int(days),
            assistant=dict(block) if isinstance(block, Mapping) else None,
            calendar=dict(calendar) if isinstance(calendar, Mapping) else {},
        )


def load_profile(data: str | Path) -> Profile:
    path = Path(data) / PROFILE_FILE
    return Profile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def ask_time(value: str) -> clock_time:
    """`ask_at` as a time. Raises `ValueError("bad_ask_at")` unless it is "HH:MM" within
    05:00-23:59: a bot day ends at 05:00, so an earlier hour would ask about the day before."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("bad_ask_at")
    at = clock_time(int(m.group(1)), int(m.group(2)))
    if at < ASK_EARLIEST:
        raise ValueError("bad_ask_at")
    return at


def validate_settings(block: object) -> dict:
    """A `settings` block (or /hora) checked all or nothing: `{"ask_at": "HH:MM",
    "period_max_day": 3..14}`, at least one key and no other. Returns the values in
    `SETTINGS_KEYS` order, `ask_at` written as HH:MM. Raises `ValueError` with a reason code,
    never the value."""
    if not isinstance(block, Mapping) or not block:
        raise ValueError("bad_settings")
    if any(key not in SETTINGS_KEYS for key in block):
        raise ValueError("unknown_key")
    out: dict = {}
    if "ask_at" in block:
        value = block["ask_at"]
        if not isinstance(value, str):
            raise ValueError("bad_ask_at")
        out["ask_at"] = ask_time(value).strftime("%H:%M")
    if "period_max_day" in block:
        value = block["period_max_day"]
        if isinstance(value, bool) or not isinstance(value, int) or value not in PERIOD_MAX_DAYS:
            raise ValueError("bad_period_max_day")
        out["period_max_day"] = value
    return out


def set_profile_lines(text: str, values: Mapping) -> str:
    """`profile.yaml` with each top-level `key:` line's value replaced, by line: every comment
    and every other line stays byte-identical, and a key that is not there is appended as a
    top-level line. `ask_at` is written quoted, since YAML 1.1 reads 21:30 as a number."""
    lines = text.splitlines(keepends=True)
    for key, value in values.items():
        rendered = f'"{value}"' if isinstance(value, str) else str(value)
        line_re = re.compile(rf"{re.escape(key)}:(?P<sp>[ \t]*)(?:[^#\r\n]*?)(?P<rest>[ \t]+#.*)?")
        found = False
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            m = line_re.fullmatch(body)
            if m is None:
                continue
            found = True
            lines[i] = f"{key}:{m['sp'] or ' '}{rendered}{m['rest'] or ''}{line[len(body) :]}"
        if not found:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}: {rendered}\n")
    return "".join(lines)


def profile_errors(p: Profile, data: str | Path) -> list[str]:
    """What keeps the bot from running on this profile, as reason codes."""
    errors = []
    if not p.name:
        errors.append("missing_name")
    try:
        ZoneInfo(p.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("bad_timezone")
    try:
        ask_time(p.ask_at)
    except ValueError:
        errors.append("bad_ask_at")
    if p.period_max_day not in PERIOD_MAX_DAYS:
        errors.append("bad_period_max_day")
    if p.chat_id is None:
        errors.append("missing_chat_id")
    if p.user_id is None:
        errors.append("missing_user_id")
    if p.history_scope not in HISTORY_SCOPES:
        errors.append("bad_history_scope")
    if p.history_days is None or not 0 <= p.history_days <= HISTORY_DAYS_MAX:
        errors.append("bad_history_days")
    titles = p.calendar.get("titles") or {}
    if not isinstance(titles, Mapping) or any(
        k not in cycle.PLAN_KINDS or not isinstance(v, str | None) for k, v in titles.items()
    ):
        errors.append("bad_calendar_titles")
    plugin = (p.assistant or {}).get("plugin")
    if plugin:
        root = Path(data).resolve()
        path = (root / str(plugin)).resolve()
        if root not in path.parents:
            errors.append("plugin_outside_data")
        elif not path.is_file():
            errors.append("plugin_missing")
    return errors


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, int | str) and str(value).lstrip("-").isdigit() else None


def _clock_text(value: object) -> str:
    """`ask_at` as "HH:MM". YAML 1.1 reads an unquoted 21:00 as the integer 1260."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value // 60:02d}:{value % 60:02d}"
    return str(value or "21:00")


# --- the worker ----------------------------------------------------------------------------


class Worker:
    """One thread that runs assistant turns, one at a time across all chats. A chat is queued
    once however many lines wait for it; a line that arrives mid-turn queues it again."""

    def __init__(self, fn: Callable, name: str = "sct-worker"):
        self._fn = fn
        self._cond = threading.Condition()
        self._queue: deque[int] = deque()
        self._busy = False
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, chat) -> None:
        """Queue a chat (or, for the `later` runner, a job: each one is a new object, so none
        is ever folded into another)."""
        with self._cond:
            if chat not in self._queue:
                self._queue.append(chat)
            self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                chat = self._queue.popleft()
                self._busy = True
            try:
                self._fn(chat)
            except Exception as e:  # noqa: BLE001 - one bad turn must not stop the worker
                log.error("ai turn crashed: %s", safe(e))
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def idle(self) -> bool:
        with self._cond:
            return not self._queue and not self._busy

    def join_idle(self, timeout: float = 10.0) -> None:
        """Wait until nothing is queued or running (tests drain the worker with this)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("worker_busy")
                self._cond.wait(remaining)

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._thread.join(timeout)


# --- callbacks -----------------------------------------------------------------------------


def parse_callback(data: object) -> tuple[str, date, str | None]:
    """`kind:YYYYMMDD[:value]` into its parts. Raises `ValueError` with a reason code."""
    if not isinstance(data, str):
        raise ValueError("bad_data")  # noqa: TRY004
    parts = data.split(":")
    kind = parts[0]
    if kind in _VALUES:
        if len(parts) != 3 or parts[2] not in _VALUES[kind]:
            raise ValueError("bad_data")
    elif kind not in _NO_VALUE or len(parts) != 2:
        raise ValueError("bad_data")
    if not _CALLBACK_DATE.match(parts[1]):
        raise ValueError("bad_date")
    try:
        d = datetime.strptime(parts[1], "%Y%m%d").date()
    except ValueError:
        raise ValueError("bad_date") from None
    return kind, d, parts[2] if len(parts) == 3 else None


@dataclass(frozen=True)
class Tap:
    id: str
    chat: int
    message_id: int
    source: str


@dataclass(frozen=True)
class Closed:
    """A period a write closed: its first and last bleed day, the `none` that closed it (None
    when the catch-up day ran out), and the prediction after it."""

    start: date
    end: date
    closing: date | None
    prediction: cycle.Prediction | None


@dataclass
class Turn:
    """One assistant turn running for a chat: the lines it started with, the lines handed to it
    while it ran, who wrote them, and the asks it posted."""

    chat: int
    ids: list
    users: list[int]
    mid: int | None  # the message an ask replies to: the newest the turn started with
    pulled: list = field(default_factory=list)
    asks: list[str] = field(default_factory=list)
    edited: dict = field(default_factory=dict)  # message id -> its newest wording
    closed: bool = False
    # taps on a calendar button: each approves the additions it named, once
    grants: list = field(default_factory=list)

    def has(self, line_id: object) -> bool:
        return line_id in self.ids or line_id in self.pulled


def _mid(line: Mapping) -> int | None:
    """The Telegram message a pending line answers to (lines from 0.2 carry only `id`)."""
    if "mid" in line:
        return line["mid"]
    return line["id"] if isinstance(line.get("id"), int) else None


def _users(lines: Sequence[Mapping]) -> list[int]:
    return list(dict.fromkeys(x["uid"] for x in lines if x.get("uid")))


def _safe_name(name: str, fallback: str) -> str:
    """A file name in plain ASCII: the stem in letters, digits, `-` and `_` (at most 60 of them,
    `fallback` when none is left) and its extension, lower case (at most 10)."""
    folded = unicodedata.normalize("NFKD", name or "")
    ascii_ = "".join(c for c in folded if not unicodedata.combining(c))
    stem, dot, ext = ascii_.rpartition(".")
    if not dot:
        stem, ext = ascii_, ""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:60] or fallback
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext)[:10].lower()
    return f"{stem}.{ext}" if ext else stem


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).isoformat(timespec="seconds")


def _grant(raw: Mapping) -> dict:
    """A turn's own copy of a calendar button's grant, so using it up marks only that copy."""
    out = dict(raw)
    if isinstance(out.get("events"), list):
        out["events"] = [dict(e) for e in out["events"] if isinstance(e, dict)]
    return out


def _same_event(event: object, entry: Mapping) -> bool:
    """Whether an ask's `event` is exactly a plan entry: the same title, start and end."""
    if not isinstance(event, Mapping):
        return False
    return all(
        str(event.get(k) or "") == str(entry.get(k) or "") for k in ("title", "start", "end")
    )


def _words(text: str) -> str:
    return " ".join(str(text or "").split())


def _key(d: date) -> str:
    return d.strftime("%Y%m%d")


def _keyboard(rows: Sequence[Sequence[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _rows(buttons: list, size: int) -> list:
    return [buttons[i : i + size] for i in range(0, len(buttons), size)]


# --- the bot -------------------------------------------------------------------------------


class Interrupted(BaseException):
    """Raised by the SIGTERM handler to cut a long poll short; never caught by `except
    Exception`."""


class Bot:
    def __init__(
        self,
        data: str | Path,
        tg,
        *,
        now: Callable[[], datetime] | None = None,
        token: str = "",
        profile: Profile | None = None,
        alive: Path | None = None,
    ):
        self.data = Path(data)
        self.tg = tg
        self.now = now or (lambda: datetime.now(UTC))
        self.profile = profile or load_profile(self.data)
        self.t = Translator(self.profile.language)
        self.repo = Repo(self.data, token)
        self.kv = KV(self.data / STATE_DB)
        self.bot_id: int | None = None
        self.username = ""
        self.read_all = False  # getMe.can_read_all_group_messages: privacy mode off
        self.last_ai: datetime | None = None  # the last successful assistant turn, for /estado
        self.alive = alive  # touched on every tick, for the container healthcheck
        self.polling = False  # inside getUpdates: SIGTERM may interrupt it
        self._backoff = BACKOFF_MIN_S
        # /nuevo bumps a chat's count; a turn that started before it does not store its id
        self._resets: Counter[int] = Counter()
        self._conv_lock = threading.Lock()
        # Spanish names first, English aliases second; the value says who may use it
        self.commands: dict[str, tuple[Callable, frozenset[str]]] = {}
        both, admins = frozenset({HER, ADMIN}), frozenset({ADMIN})
        for names, handler, who in (
            (("hoy", "today"), self.cmd_hoy, both),
            (("resumen", "summary"), self.cmd_resumen, both),
            (("anotar", "log"), self.cmd_anotar, both),
            (("deshacer", "undo"), self.cmd_deshacer, both),
            (("recordatorios", "reminders"), self.cmd_recordatorios, both),
            (("hora", "time"), self.cmd_hora, both),
            (("estado", "status"), self.cmd_estado, admins),
            (("nuevo", "new"), self.cmd_nuevo, admins),
            (("presentacion", "about"), self.cmd_presentacion, admins),
            (("ayuda", "help"), self.cmd_ayuda, both),
        ):
            for name in names:
                self.commands[name] = (handler, who)
        self.worker = Worker(self._turn)
        # a plugin's slow work (`later`): one job at a time across every chat, in order; a
        # restart loses what is queued or running
        self.later = Worker(lambda job: job(), name="sct-later")
        # One answer per burst. `_q` guards what the poll loop, the worker, the hearing thread and
        # the plugin's thread share: when each chat's newest line came, the notes still being
        # heard, the turn running per chat and the plugin's open asks. Taking the KV lock while
        # holding `_q` is fine; the other way round never happens.
        self.debounce_s, self.voice_wait_s = DEBOUNCE_S, VOICE_WAIT_S
        self.clock, self.sleep = time.monotonic, time.sleep
        self._q = threading.Lock()
        self._newest: dict[int, float] = {}
        self._voicing: dict[int, dict] = {}  # chat -> {line id: when the note arrived}
        self._running: dict[int, Turn] = {}
        self.asks: dict[str, dict] = {}  # the plugin's open asks, by the key on their buttons
        self.hearer = Worker(self._hear_chat, name="sct-hear")
        self.side = Worker(lambda job: job(), name="sct-asks")  # answers to asks, off the poll loop

    def start(self) -> None:
        """`getMe`, then `getChatMember` for the bot in the group: Telegram delivers every group
        message to a bot that is privacy-off *or* a group administrator, and `getMe` only tells
        the first. Either one turns on the privacy-off rule (`read_all`)."""
        me = self.tg.call("getMe") or {}
        self.bot_id = me.get("id")
        self.username = str(me.get("username") or "")
        reasons = []
        if me.get("can_read_all_group_messages"):
            reasons.append("privacy mode off")
        if self.group_admin():
            reasons.append("group admin")
        self.read_all = bool(reasons)
        log.info("bot @%s up", self.username)
        if reasons:
            log.info("group privacy off: every group message arrives (%s)", ", ".join(reasons))
        else:
            log.info("group privacy on")

    def group_admin(self) -> bool:
        """Whether the bot is an administrator (or the creator) of the configured group. A
        failed call counts as no, so the bot still starts."""
        if self.bot_id is None or self.chat_id is None:
            return False
        try:
            member = self.tg.call("getChatMember", chat_id=self.chat_id, user_id=self.bot_id)
        except TelegramError as e:
            log.warning("getChatMember failed: %s", safe(e))
            return False
        return isinstance(member, Mapping) and member.get("status") in ADMIN_STATUSES

    def set_commands(self) -> None:
        """The menu: one `setMyCommands` in the default scope with the five she uses. The admin
        commands are listed by /ayuda when an admin asks."""
        menu = self.t("menu")
        commands = [{"command": name, "description": menu[name]} for name in MENU]
        try:
            self.tg.call("setMyCommands", commands=commands)
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", safe(e))

    def close(self) -> None:
        self.hearer.stop()
        self.worker.stop()
        self.later.stop()
        self.side.stop()
        self.kv.close()

    def join_idle(self, timeout: float = 10.0) -> None:
        """Wait until no note is being heard, no turn runs and no answer to an ask is on its way
        (tests drain the bot with this)."""
        deadline = time.monotonic() + timeout
        workers = (self.hearer, self.worker, self.side)
        while not all(w.idle() for w in workers):
            for w in workers:
                w.join_idle(max(0.01, deadline - time.monotonic()))

    # --- small helpers -----------------------------------------------------------------

    @property
    def chat_id(self) -> int | None:
        migrated = self.kv.get("migrated_chat")
        return int(migrated) if migrated else self.profile.chat_id

    @property
    def has_assistant(self) -> bool:
        return bool(self.profile.assistant and self.profile.assistant.get("plugin"))

    def today(self) -> date:
        return cycle.bot_day(self.now(), self.profile.timezone)

    def role(self, user: object) -> str | None:
        if user is not None and user == self.profile.user_id:
            return HER
        if user in self.profile.admins:
            return ADMIN
        return None

    def who(self, role: str, user: object) -> str:
        return self.profile.name if role == HER else self.profile.admins.get(user, "")

    def mention(self) -> str:
        """`@username` when the profile has one (Telegram's own selective target), else an
        HTML text mention."""
        if self.profile.username:
            return "@" + self.profile.username
        name = html.escape(self.profile.name)
        return f'<a href="tg://user?id={self.profile.user_id}">{name}</a>'

    def skipped(self) -> set[date]:
        return {date.fromisoformat(k.removeprefix("skip:")) for k in self.kv.keys("skip:")}

    @property
    def max_day(self) -> int:
        """The last period day the bot asks about daily (`period_max_day`)."""
        return self.profile.period_max_day or cycle.PERIOD_MAX_DAY

    def phase(self, log_: Log, today: date) -> cycle.Phase:
        return cycle.phase(log_, today, self.skipped(), self.max_day)

    def queue(self, ph: cycle.Phase, log_: Log, today: date) -> tuple[date, ...]:
        return cycle.queue(ph, log_, today, self.max_day)

    def summary(self, day: Day | None) -> str:
        """«normal, dolor 4, calambres»: the day in her words, no note."""
        if day is None:
            return "-"
        parts = []
        if day.flow is not None:
            parts.append(self.t.flow(day.flow))
        if day.pain is not None:
            parts.append(self.t("pain", n=day.pain))
        parts += [self.t.symptom(s) for s in day.symptoms]
        return ", ".join(parts) or "-"

    @staticmethod
    def period_day(d: date, log_: Log) -> int | None:
        first = [s for s in records.starts(log_) if s <= d]
        return (d - first[-1]).days + 1 if first else None

    # --- Telegram ----------------------------------------------------------------------

    def send(self, chat: int, text: str, markup: dict | None = None, **extra) -> dict | None:
        return self.tg.call(
            "sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup=markup, **extra
        )

    def post(self, chat: int, text: str, markup: dict | None = None, **extra) -> dict | None:
        """`send` that logs a failure instead of raising (the worker and follow-up lines)."""
        try:
            return self.send(chat, text, markup, **extra)
        except TelegramError as e:
            log.warning("send failed: %s", safe(e))
            return None

    def answer(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:
            log.warning("answerCallbackQuery failed: %s", safe(e))

    def edit(self, tap: Tap, text: str, markup: dict | None = None) -> None:
        """Edit the card in place. "message is not modified" is fine; any other failure sends
        the step as a new message."""
        try:
            self.tg.call(
                "editMessageText",
                chat_id=tap.chat,
                message_id=tap.message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except TelegramError as e:
            if e.code == 400 and NOT_MODIFIED in e.description:
                return
            log.warning("edit failed: %s", safe(e))
            self.post(tap.chat, text, markup)

    # --- keyboards ---------------------------------------------------------------------

    def kb_waiting(self, d: date) -> dict:
        t, k = self.t, _key(d)
        return _keyboard(
            [
                [
                    (t("btn_yes_light"), f"y:{k}:light"),
                    (t("btn_yes_medium"), f"y:{k}:medium"),
                    (t("btn_yes_heavy"), f"y:{k}:heavy"),
                ],
                [(t("btn_yes_other"), f"pick:{k}"), (t("btn_not_yet"), f"wait:{k}")],
                [(t("btn_tell_more"), f"more:{k}")],
            ]
        )

    def kb_period(self, d: date, log_: Log, today: date) -> dict:
        t, k = self.t, _key(d)
        rows = [
            [
                (t("btn_light"), f"f:{k}:light"),
                (t("btn_medium"), f"f:{k}:medium"),
                (t("btn_heavy"), f"f:{k}:heavy"),
            ]
        ]
        previous = log_.get(d - timedelta(days=1))
        if previous is not None and previous.flow is not None:
            rows.append([(t("btn_same"), f"f:{k}:same")])
        if d == today:
            rows.append([(t("btn_stopped"), f"f:{k}:none"), (t("btn_tell_more"), f"more:{k}")])
        else:
            rows.append(
                [(t("btn_stopped_then"), f"f:{k}:none"), (t("btn_dont_remember"), f"skip:{k}")]
            )
        return _keyboard(rows)

    def kb_pain(self, d: date) -> dict:
        k = _key(d)
        return _keyboard(
            [
                [(str(n), f"p:{k}:{n}") for n in range(6)],
                [(str(n), f"p:{k}:{n}") for n in (6, 7, 8, 9, 10)],
            ]
        )

    def top_symptoms(self, log_: Log) -> list[str]:
        """Her most frequent symptoms in the log, padded with the defaults in their order."""
        return _top(Counter(s for day in log_.values() for s in day.symptoms), DEFAULT_SYMPTOMS)

    def eve_symptoms(self, log_: Log) -> list[str]:
        """Her most frequent symptoms on the day before a start, padded with the eve defaults."""
        return _top(eve_counts(log_, (1,)), DEFAULT_EVE_SYMPTOMS)

    def _toggles(self, kind: str, d: date, log_: Log, shown: list[str]) -> list:
        """The symptom toggle rows for date `d`: a chosen one shows «✓», and one chosen in
        words that is not among the six is added after them."""
        chosen = set(log_[d].symptoms) if d in log_ else set()
        shown = shown + [s for s in SYMPTOMS if s in chosen and s not in shown]
        k = _key(d)
        buttons = [
            (("✓ " if s in chosen else "") + self.t.symptom(s), f"{kind}:{k}:{s}") for s in shown
        ]
        return _rows(buttons, 3)

    def kb_symptoms(self, d: date, log_: Log) -> dict:
        t, k = self.t, _key(d)
        rows = self._toggles("s", d, log_, self.top_symptoms(log_))
        rows.append([(t("btn_other"), f"more:{k}"), (t("btn_done"), f"d:{k}")])
        return _keyboard(rows)

    def kb_eve(self, eve: date, log_: Log) -> dict:
        """The eve step's keyboard, bound to the day before the start: «Nada especial» and
        «Listo» both finish it."""
        t, k = self.t, _key(eve)
        rows = self._toggles("e", eve, log_, self.eve_symptoms(log_))
        rows.append([(t("btn_nothing"), f"ed:{k}"), (t("btn_done"), f"ed:{k}")])
        return _keyboard(rows)

    def kb_done(self, d: date) -> dict:
        k = _key(d)
        return _keyboard(
            [[(self.t("btn_fix"), f"fix:{k}"), (self.t("btn_tell_more"), f"more:{k}")]]
        )

    def kb_pick(self, d: date) -> dict:
        days = [d - timedelta(days=i) for i in range(PICK_DAYS, 0, -1)]
        buttons = [(self.t.date(x, "pick"), f"y:{_key(x)}:unspecified") for x in days]
        rows = _rows(buttons, 3)
        rows.append([(self.t("btn_back"), f"back:{_key(d)}")])
        return _keyboard(rows)

    # --- card texts --------------------------------------------------------------------

    def ask_text(self, d: date, log_: Log, today: date, ph: cycle.Phase) -> str:
        """The flow step's question for date `d`."""
        t, m = self.t, self.mention()
        if d == today:
            n = ph.day if ph.kind == "period" else self.period_day(d, log_)
            return t("ask_period", m=m, n=n) if n else t("ask_late", m=m)
        if ph.kind == "period" and ph.day == cycle.catchup_day(self.max_day):
            return t("ask_catchup", m=m, date=t.date(d))
        if d == today - timedelta(days=1):
            return t("ask_gap_one", m=m, date=t.date(d))
        return t("ask_gap_many", m=m, date=t.date(d))

    def waiting_text(self, ph: cycle.Phase) -> str:
        key = "ask_waiting" if ph.kind == "waiting" else "ask_late"
        return self.t(key, m=self.mention())

    def card(self, today: date) -> tuple[str, dict]:
        """Today's card: in a period the first queued date (missing days first, then today),
        otherwise the «¿Te ha bajado ya?» card."""
        log_ = self.repo.load()
        ph = self.phase(log_, today)
        if ph.kind == "period":
            dates = self.queue(ph, log_, today)
            d = dates[0] if dates else today
            return self.ask_text(d, log_, today, ph), self.kb_period(d, log_, today)
        return self.waiting_text(ph), self.kb_waiting(today)

    def today_keyboard(self) -> dict | None:
        try:
            return self.card(self.today())[1]
        except ValueError as e:  # an unreadable log: no keyboard rather than no message
            log.warning("no keyboard: %s", safe(e))
            return None

    def send_card(self, chat: int, today: date, suffix: str = "") -> dict | None:
        text, markup = self.card(today)
        sent = self.send(chat, text + suffix, markup)
        log.info("ask sent")
        return sent

    def done_line(self, d: date, log_: Log) -> str:
        return f"{_cap(self.t.date(d))}: {self.summary(log_.get(d))}."

    def done_text(self, d: date, log_: Log, today: date) -> str:
        t = self.t
        ph = self.phase(log_, today)
        n = self.period_day(d, log_)
        summary = _cap(self.summary(log_.get(d))) + "."
        if n is None:
            text = t("toast_saved", what=summary)
        else:
            text = t("card_done", date=t.date(d), n=n, summary=summary)
        if ph.kind == "period" and ph.day == self.max_day:
            text += " " + t("day7", n=self.max_day)
        elif ph.kind == "period" and ph.day is not None and ph.day < self.max_day:
            text += " " + t("tomorrow", time=self.profile.ask_at)
        return text

    # --- updates -----------------------------------------------------------------------

    def handle(self, update: Mapping) -> None:
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])
        elif "edited_message" in update:
            self.on_edit(update["edited_message"])

    def on_callback(self, cq: Mapping) -> None:
        t = self.t
        user = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        role = self.role(user)
        if role is None:
            self.answer(cq["id"], t("only_her", name=self.profile.name))
            log.info("callback refused: not_allowed")
            return
        data = cq.get("data")
        if isinstance(data, str) and data.startswith(("a:", "o:", "r:", "c:", "k:")):
            if chat is None:
                self.answer(cq["id"])
            elif data.startswith("a:"):
                self.tapped_ask(cq, chat, data, user)
            elif data.startswith("r:"):
                self.tapped_remind(cq, chat, data, user, role)
            elif data.startswith("c:"):
                self.tapped_plan(cq, chat, data, user, role)
            elif data.startswith("k:"):
                self.tapped_cancel(cq, chat, data)
            else:
                self.tapped_option(cq, chat, data, user, role)
            return
        try:
            kind, d, value = parse_callback(cq.get("data"))
        except ValueError as e:
            self.answer(cq["id"])
            log.info("callback refused: %s", e)
            return
        today = self.today()
        if not records.in_range(d, today):
            self.answer(cq["id"], t("toast_error" if d > today else "toast_old"))
            log.info("callback refused: date_out_of_range")
            return
        if chat is None or message.get("message_id") is None:
            self.answer(cq["id"])
            return
        tap = Tap(cq["id"], chat, message["message_id"], "telegram" if role == HER else "admin")
        try:
            getattr(self, f"_tap_{kind}")(tap, d, value, today)
        except ValueError as e:  # an unreadable log, or «Igual que ayer» with no yesterday
            self.answer(tap.id, t("toast_error"))
            log.warning("callback failed: %s", safe(e))
            return
        log.info("callback ok")

    # --- taps ----------------------------------------------------------------------------

    def write(self, fn: Callable[[Log], Change]) -> Change:
        change = self.repo.write(fn)
        if change:
            log.info("record written days=%d", len(change.items))
        return change

    def _set(self, tap: Tap, d: date, fields: Mapping) -> Change:
        edit = Edit(days={d: fields})
        return self.write(lambda log_: records.apply(log_, edit, tap.source))

    def _tap_y(self, tap: Tap, d: date, value: str, today: date) -> None:
        change = self._set(tap, d, {"flow": value})
        self.answer(tap.id, self.t("toast_saved", what=self.t.flow(value)))
        if value == "unspecified":  # from the picker: now how much, then the gaps up to today
            self.flow_step(tap, d, change.after, today)
        else:
            self.edit(
                tap, self.t("ask_pain", done=self.done_line(d, change.after)), self.kb_pain(d)
            )
        self.announce(change, tap.chat)

    def _tap_f(self, tap: Tap, d: date, value: str, today: date) -> None:
        if value == "same":

            def same(log_: Log) -> Change:
                previous = log_.get(d - timedelta(days=1))
                if previous is None or previous.flow is None:
                    raise ValueError("no_previous_day")
                fields = {"flow": previous.flow, "pain": previous.pain}
                fields["symptoms"] = previous.symptoms
                return records.apply(log_, Edit(days={d: fields}), tap.source)

            change = self.write(same)
            what = self.summary(change.after.get(d))
        else:
            change = self._set(tap, d, {"flow": value})
            what = self.t.flow(value)
        self.answer(tap.id, self.t("toast_saved", what=what))
        if value in ("none", "same"):
            self.advance(tap, d, change.after, today)
        else:
            self.edit(
                tap, self.t("ask_pain", done=self.done_line(d, change.after)), self.kb_pain(d)
            )
        self.announce(change, tap.chat)

    def _tap_p(self, tap: Tap, d: date, value: str, today: date) -> None:
        pain = int(value)
        change = self._set(tap, d, {"pain": pain})
        self.answer(tap.id, self.t("toast_saved", what=self.t("pain", n=pain)))
        self.symptoms_step(tap, d, change.after)
        self.announce(change, tap.chat)

    def toggle(self, tap: Tap, d: date, value: str) -> Change:
        """Add or remove one symptom on date `d`."""

        def flip(log_: Log) -> Change:
            current = set(log_[d].symptoms) if d in log_ else set()
            current ^= {value}
            return records.apply(log_, Edit(days={d: {"symptoms": tuple(current)}}), tap.source)

        change = self.write(flip)
        self.answer(tap.id, self.t("toast_saved", what=self.t.symptom(value)))
        return change

    def _tap_s(self, tap: Tap, d: date, value: str, today: date) -> None:
        change = self.toggle(tap, d, value)
        self.symptoms_step(tap, d, change.after)
        self.announce(change, tap.chat)

    def _tap_d(self, tap: Tap, d: date, value: None, today: date) -> None:
        """«Listo» on the symptoms step: the eve step when `d` is a start and the day before
        says nothing yet, else the next queued date or the done step."""
        self.answer(tap.id)
        log_ = self.repo.load()
        if self.wants_eve(d, log_, today):
            self.eve_step(tap, d, log_)
            return
        self.advance(tap, d, log_, today)

    def _tap_e(self, tap: Tap, eve: date, value: str, today: date) -> None:
        """A symptom on the day before a start: a symptom-only day, which never moves a start."""
        change = self.toggle(tap, eve, value)
        self.eve_step(tap, eve + timedelta(days=1), change.after)
        self.announce(change, tap.chat)

    def _tap_ed(self, tap: Tap, eve: date, value: None, today: date) -> None:
        """«Nada especial» or «Listo» on the eve step: on to the start's done step."""
        self.answer(tap.id)
        self.advance(tap, eve + timedelta(days=1), self.repo.load(), today)

    def _tap_pick(self, tap: Tap, d: date, value: None, today: date) -> None:
        self.answer(tap.id)
        self.edit(tap, self.t("pick_day"), self.kb_pick(d))

    def _tap_back(self, tap: Tap, d: date, value: None, today: date) -> None:
        self.answer(tap.id)
        ph = self.phase(self.repo.load(), today)
        self.edit(tap, self.waiting_text(ph), self.kb_waiting(d))

    def _tap_wait(self, tap: Tap, d: date, value: None, today: date) -> None:
        self.answer(tap.id)
        self.edit(tap, self.t("not_yet"))

    def _tap_skip(self, tap: Tap, d: date, value: None, today: date) -> None:
        self.kv.set(f"skip:{d.isoformat()}", "1")
        self.answer(tap.id)
        log_ = self.repo.load()
        text = self.t("skipped", date=self.t.date(d))
        nxt = self.next_date(d, log_, today)
        if nxt is None:
            self.edit(tap, text)
            return
        ph = self.phase(log_, today)
        question = self.ask_text(nxt, log_, today, ph)
        self.edit(tap, f"{text}\n\n{question}", self.kb_period(nxt, log_, today))

    def _tap_fix(self, tap: Tap, d: date, value: None, today: date) -> None:
        """«Corregir»: the flow step again, with a question that fits a day already answered."""
        self.answer(tap.id)
        log_ = self.repo.load()
        text = self.t("ask_fix", m=self.mention(), date=self.t.date(d))
        self.edit(tap, text, self.kb_period(d, log_, today))

    def _tap_more(self, tap: Tap, d: date, value: None, today: date) -> None:
        self.answer(tap.id)
        self.tell_me(tap.chat)

    # --- steps ---------------------------------------------------------------------------

    def flow_step(self, tap: Tap, d: date, log_: Log, today: date) -> None:
        ph = self.phase(log_, today)
        self.edit(tap, self.ask_text(d, log_, today, ph), self.kb_period(d, log_, today))

    def symptoms_step(self, tap: Tap, d: date, log_: Log) -> None:
        text = self.t("ask_symptoms", done=self.done_line(d, log_))
        self.edit(tap, text, self.kb_symptoms(d, log_))

    def wants_eve(self, d: date, log_: Log, today: date) -> bool:
        """Whether the card asks about the day before `d`: `d` is a start, and that day has no
        flow and no symptom yet and is recent enough for a tap to record it."""
        day = log_.get(d)
        if day is None or not day.cycle_start:
            return False
        eve = d - timedelta(days=1)
        before = log_.get(eve)
        if before is not None and (before.flow is not None or before.symptoms):
            return False
        return records.in_range(eve, today)

    def eve_step(self, tap: Tap, d: date, log_: Log) -> None:
        eve = d - timedelta(days=1)
        text = self.t("ask_eve", done=self.done_line(d, log_), date=self.t.date(eve))
        self.edit(tap, text, self.kb_eve(eve, log_))

    def next_date(self, d: date, log_: Log, today: date) -> date | None:
        ph = self.phase(log_, today)
        dates = [x for x in self.queue(ph, log_, today) if x != d]
        return dates[0] if dates else None

    def advance(self, tap: Tap, d: date, log_: Log, today: date) -> None:
        """After a date is done: the next queued date (missing days first, then today) in the
        same message, or the done step."""
        nxt = self.next_date(d, log_, today)
        if nxt is None:
            self.edit(tap, self.done_text(d, log_, today), self.kb_done(d))
            return
        key = "next_today" if nxt == today else "next_gap"
        text = self.t(key, date=self.t.date(d), next=self.t.date(nxt))
        self.edit(tap, text, self.kb_period(nxt, log_, today))

    def tell_me(self, chat: int) -> None:
        """«Contarte más»: a prompt that mentions her with a selective force_reply, so her
        client opens the reply box and her words arrive as a reply to the bot."""
        markup = {
            "force_reply": True,
            "selective": True,
            "input_field_placeholder": self.t("placeholder"),
        }
        self.post(chat, self.t("tell_me", m=self.mention()), markup)

    # --- what a write says ---------------------------------------------------------------

    def closed(self, change: Change) -> Closed | None:
        """The period a write closed: open before it, not after it, with the same start."""
        today = self.today()
        before = self.phase(change.before, today)
        after = self.phase(change.after, today)
        if before.kind != "period" or after.kind in ("period", "no_history"):
            return None
        if after.start is None or after.start != before.start:
            return None
        start, log_ = after.start, change.after
        flows = sorted(d for d, day in log_.items() if start <= d <= today and day.flow is not None)
        bleeds = [d for d in flows if log_[d].bleed]
        end = bleeds[-1] if bleeds else start
        closing = next((d for d in flows if d > end and log_[d].flow == "none"), None)
        return Closed(start, end, closing, after.prediction)

    def announce(self, change: Change, chat: int) -> None:
        """The closing line and the pain red flag, from the transition in the `Change`: no flag
        is stored, so a close that is undone and redone posts again."""
        if not change:
            return
        t = self.t
        done = self.closed(change)
        if done is not None and done.prediction is not None:
            p = done.prediction
            self.post(
                chat,
                t(
                    "closed",
                    days=(done.end - done.start).days + 1,
                    start=t.date(done.start, "short"),
                    end=t.date(done.end, "short"),
                    expected=t.date(p.expected, "short"),
                    ws=t.date(p.window_start, "short"),
                    we=t.date(p.window_end, "short"),
                ),
            )
        crossings = [
            after.pain
            for _, before, after in change.items
            if after is not None
            and after.pain is not None
            and after.pain >= PAIN_HIGH
            and (before is None or before.pain is None or before.pain < PAIN_HIGH)
        ]
        if crossings:
            self.post(chat, t("pain_high", n=max(crossings)))

    def recorded_text(self, change: Change) -> str | None:
        """«Anotado:» with one line per date that changed, built from the `Change` and never
        from the assistant's prose. None when nothing she would see changed."""
        t = self.t
        done = self.closed(change)
        lines = []
        for d, before, after in change.items:
            when = t.date(d)
            if after is None:
                lines.append(t("line_deleted", date=when))
            elif done is not None and d == done.closing:
                lines.append(t("line_closed", date=when))
            elif before is not None and _same_facts(before, after):
                continue  # only the source moved (her words resent by an admin)
            elif self.summary(after) == (self.summary(before) if before else "-"):
                lines.append(t("line_note", date=when))  # the summary leaves the note out
            elif before is None:
                lines.append(t("line_new", date=when, summary=self.summary(after)))
            else:
                lines.append(
                    t(
                        "line_changed",
                        date=when,
                        before=self.summary(before),
                        after=self.summary(after),
                    )
                )
        return "\n".join([t("recorded"), *lines]) if lines else None

    # --- messages ------------------------------------------------------------------------

    def on_message(self, msg: Mapping) -> None:
        if msg.get("migrate_to_chat_id") is not None:
            self.migrate(msg)
            return
        if msg.get("voice") or msg.get("audio"):
            self.on_voice(msg)
            return
        if msg.get("photo") or msg.get("document"):
            self.on_file(msg)
            return
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        chat = msg.get("chat") or {}
        user = (msg.get("from") or {}).get("id")
        role = self.role(user)
        private = chat.get("type") == "private"
        if not private and chat.get("id") != self.chat_id:
            return
        if role is None:  # anyone else gets no answer; in the group, the log may keep it
            if not private:
                self.log_seen(chat["id"], msg, text)
            return
        command = _COMMAND.match(text.strip())
        if command:
            name, handle, rest = command.group(1).lower(), command.group(2), command.group(3)
            if handle and handle.lower() != self.username.lower():
                return  # a command for another bot
            entry = self.commands.get(name)
            if entry is not None and role in entry[1]:
                entry[0](msg, (rest or "").strip(), role)
                log.info("command %s", entry[0].__name__.removeprefix("cmd_"))
            return
        content = self.addressed(msg, text, private, role)
        if content is not None:
            self.queue_text(chat["id"], msg, content, role, self._strip_mention(msg, text)[0])
        elif not private:
            self.log_seen(chat["id"], msg, text)

    def sender(self, msg: Mapping) -> str:
        """How the log names a message's sender: her or an admin by the profile, anyone else by
        their Telegram first name."""
        frm = msg.get("from") or {}
        role = self.role(frm.get("id"))
        if role is not None:
            return self.who(role, frm.get("id"))
        return " ".join(str(frm.get("first_name") or "?").split())[:64]

    def log_seen(self, chat: int, msg: Mapping, text: str) -> None:
        """A group message that does not call the bot. With `history_scope: all` the chat log
        keeps it, and it goes to the next turn as context marked «[not to you]»."""
        if self.profile.history_scope != "all" or not self.has_assistant:
            return
        body = text.strip()
        if body.startswith("/"):
            return
        name = self.sender(msg)
        now = int(self.now().timestamp())
        mid = msg.get("message_id")
        self.kv.add_chat(
            chat, "user", name, f"{name}: {body}", now, body=body, mid=mid, asked=False
        )
        log.info("group line logged")

    def addressed(self, msg: Mapping, text: str, private: bool, role: str) -> str | None:
        """The line for the assistant when the message is meant for the bot, else None.

        Meant for the bot: a private chat; a reply to any bot message; a message that mentions
        the bot (the mention is stripped); and with privacy off, every text from her in the
        group. An admin's plain group text never is, so the household can talk in the group."""
        reply = msg.get("reply_to_message") or {}
        to_bot = self.bot_id is not None and (reply.get("from") or {}).get("id") == self.bot_id
        body, mentioned = self._strip_mention(msg, text)
        if not (private or to_bot or mentioned or (self.read_all and role == HER)):
            return None
        body = body.strip()
        if not body:
            return None
        user = (msg.get("from") or {}).get("id")
        prefix = f"{self.who(role, user)}: "
        if to_bot:
            quoted = reply.get("text") or reply.get("caption") or ""
            prefix += f"[replying to the bot: {quoted}] "
        return prefix + body

    def _strip_mention(
        self, msg: Mapping, text: str, entities: str = "entities"
    ) -> tuple[str, bool]:
        """Remove each `mention` entity equal to the bot's @username (`caption_entities` for a
        caption). Entity offsets count UTF-16 code units, so the text is cut in UTF-16."""
        if not self.username:
            return text, False
        raw = text.encode("utf-16-le")
        target = "@" + self.username.lower()
        spans = []
        for e in msg.get(entities) or []:
            if e.get("type") != "mention":
                continue
            a, b = 2 * int(e.get("offset", 0)), 2 * int(e.get("offset", 0) + e.get("length", 0))
            if raw[a:b].decode("utf-16-le", "replace").lower() == target:
                spans.append((a, b))
        for a, b in sorted(spans, reverse=True):
            raw = raw[:a] + raw[b:]
        return raw.decode("utf-16-le", "replace"), bool(spans)

    def on_voice(self, msg: Mapping) -> None:
        """A voice note (or an audio file) from her or an admin, in the group or a private chat.
        It is always for the bot, whatever it replies to: a voice note cannot carry a mention.
        It is queued like a text line and the worker turns it into one (`hear`)."""
        chat = msg.get("chat") or {}
        user = (msg.get("from") or {}).get("id")
        role = self.role(user)
        if role is None:
            return
        if chat.get("type") != "private" and chat.get("id") != self.chat_id:
            return
        media = msg.get("voice") or msg.get("audio")
        if not isinstance(media, Mapping) or not media.get("file_id"):
            return
        note = {"message_id": msg.get("message_id"), "allow_sending_without_reply": True}
        if not self.has_assistant:
            self.post(chat["id"], self.t("voice_off"), reply_parameters=note)
            log.info("voice without assistant")
            return
        size = media.get("file_size")
        if isinstance(size, int) and size > VOICE_MAX:
            self.post(chat["id"], self.t("voice_too_long"), reply_parameters=note)
            log.info("voice refused: too_long")
            return
        voice = {"file_id": media["file_id"], "mime": str(media.get("mime_type") or "audio/ogg")}
        line = {
            "id": msg.get("message_id"),
            "ts": int(self.now().timestamp()),
            "voice": voice,
            "who": self.who(role, user),
            "admin": role == ADMIN,
            "uid": user,
        }
        self.queue_line(chat["id"], line)
        log.info("voice queued")

    def on_file(self, msg: Mapping) -> None:
        """A photo or a file of any kind from her or an admin, in the group or a private chat.
        Like a voice note it is always for the bot: a photo of a test or a report dropped in the
        group is meant for it. Its caption is the message. It is queued as a text line naming the
        file, `inbox/<message id>-<name>` with its own name and extension; the turn that takes it
        downloads it (`fetch_files`). Whether the assistant can open it is the plugin's call."""
        chat = msg.get("chat") or {}
        user = (msg.get("from") or {}).get("id")
        role = self.role(user)
        if role is None:
            return
        if chat.get("type") != "private" and chat.get("id") != self.chat_id:
            return
        mid = msg.get("message_id")
        note = {"message_id": mid, "allow_sending_without_reply": True}
        if msg.get("photo"):
            sizes = [s for s in msg["photo"] if isinstance(s, Mapping) and s.get("file_id")]
            if not sizes:
                return
            media = max(sizes, key=lambda s: (s.get("file_size") or 0, s.get("width") or 0))
            name, mime, label = f"inbox/{mid}-foto.jpg", "image/jpeg", "photo"
        else:
            media = msg["document"]
            if not isinstance(media, Mapping) or not media.get("file_id"):
                return
            mime = str(media.get("mime_type") or "application/octet-stream").lower()
            safe = _safe_name(str(media.get("file_name") or ""), "archivo")
            name, label = f"inbox/{mid}-{safe}", "file"
        size = media.get("file_size")
        if isinstance(size, int) and size > FILE_MAX:
            self.post(chat["id"], self.t("file_too_big"), reply_parameters=note)
            log.info("file refused: too_big")
            return
        body = self._strip_mention(msg, str(msg.get("caption") or ""), "caption_entities")[0]
        body = body.strip()
        content = f"{self.who(role, user)}: [{label}: {name}] {body}".rstrip()
        file = {"file_id": media["file_id"], "name": name, "mime": mime, "size": size}
        self.queue_text(chat["id"], msg, content, role, body, file=file)

    def queue_text(
        self,
        chat: int,
        msg: Mapping,
        content: str,
        role: str,
        body: str | None = None,
        row: int | None = None,
        file: dict | None = None,
    ) -> None:
        """Append a line to `pending:<chat>`, log it in the chat log (or reuse its `row`) and
        queue the chat for the worker. `body` is the message's own words, kept so an edit can be
        compared with them; `file` is the photo or file the line names, fetched when its turn
        starts. Without an assistant: «no_ai» with today's keyboard."""
        if not self.has_assistant:
            self.post(chat, self.t("no_ai"), self.today_keyboard())
            log.info("text without assistant")
            return
        now = int(self.now().timestamp())
        user = (msg.get("from") or {}).get("id")
        if row is None:
            row = self.kv.add_chat(
                chat,
                "user",
                self.who(role, user),
                content,
                now,
                body=body.strip() if body is not None else None,
                mid=msg.get("message_id"),
            )
        line = {
            "id": msg.get("message_id"),
            "ts": now,
            "content": content,
            "admin": role == ADMIN,
            "uid": user,
            "row": row,
        }
        if body is not None:
            line["body"] = body.strip()
        if file is not None:
            line["file"] = file
        self.queue_line(chat, line)
        log.info("file queued" if file is not None else "text queued")

    def queue_line(self, chat: int, line: dict) -> None:
        """Append `line` to `pending:<chat>` (the newest 10, none older than 7 days) and queue
        the chat: a voice note for the hearing thread, text for the worker."""
        now = int(self.now().timestamp())
        dropped = []

        def add(lines):
            lines = [*(lines or []), line]
            kept = [x for x in lines if now - int(x.get("ts", 0)) <= PENDING_MAX_AGE_S]
            kept = kept[-PENDING_MAX:]
            dropped.append(len(lines) - len(kept))
            return kept

        self.kv.update_json(f"pending:{chat}", add, [])
        if dropped and dropped[0]:
            log.info("pending capped dropped=%d", dropped[0])
        with self._q:
            self._newest[chat] = self.clock()
            if "voice" in line:
                self._voicing.setdefault(chat, {})[line["id"]] = self.clock()
        if "voice" in line:
            self.hearer.submit(chat)
        else:
            self.worker.submit(chat)

    def cmd_hoy(self, msg: Mapping, rest: str, role: str) -> None:
        """/hoy: today's card; with text after it, the text goes to the assistant."""
        chat = msg["chat"]["id"]
        if rest:
            user = (msg.get("from") or {}).get("id")
            self.queue_text(chat, msg, f"{self.who(role, user)}: {rest}", role, rest)
        else:
            self.send_card(chat, self.today())

    def cmd_resumen(self, msg: Mapping, rest: str, role: str) -> None:
        """/resumen: last period, recent lengths, usual length, next window, the symptoms she
        logs most. No AI."""
        self.send(msg["chat"]["id"], self.summary_text(self.repo.load(), self.today()))

    def summary_text(self, log_: Log, today: date) -> str:
        t = self.t
        p = cycle.predict(log_, today)
        if p is None:
            return t("summary_empty")
        logged = sum(1 for d, x in log_.items() if p.last_start <= d <= today and x.bleed)
        lines = [
            t("summary_title", name=html.escape(self.profile.name)),
            t("summary_last", start=t.date(p.last_start, "short"), days=logged),
        ]
        if len(p.lengths) >= cycle.MIN_LENGTHS:
            lengths = ", ".join(str(n) for n in p.lengths)
            lines.append(t("summary_lengths", lengths=lengths, median=p.median))
        lines.append(t("summary_period", days=p.period_days))
        lines.append(
            t(
                "summary_next",
                expected=t.date(p.expected, "short"),
                ws=t.date(p.window_start, "short"),
                we=t.date(p.window_end, "short"),
            )
        )
        past = {d: x for d, x in log_.items() if d <= today}
        counts = Counter(s for x in past.values() for s in x.symptoms)
        if counts:
            top = sorted(counts, key=lambda s: (-counts[s], SYMPTOMS.index(s)))[:3]
            lines.append(t("summary_symptoms", items=", ".join(t.symptom(s) for s in top)))
        if eve_starts(past) >= EVE_SUMMARY_MIN:
            eve = eve_counts(past, (1, 2))
            top = sorted(eve, key=lambda s: (-eve[s], SYMPTOMS.index(s)))[:3]
            lines.append(t("summary_eve", items=", ".join(t.symptom(s) for s in top)))
        return "\n".join(lines)

    def cmd_anotar(self, msg: Mapping, rest: str, role: str) -> None:
        """/anotar <cuándo> <cuánto> [dolor] [síntomas…]: a day without the assistant."""
        chat = msg["chat"]["id"]
        today = self.today()
        try:
            d, fields = parse_anotar(rest, self.t, today)
        except ValueError:
            self.send(chat, self.t("anotar_usage"))
            log.info("anotar refused")
            return
        source = "telegram" if role == HER else "admin"
        change = self.write(lambda log_: records.apply(log_, Edit(days={d: fields}), source))
        what = f"{self.t.date(d)}, {self.summary(change.after.get(d))}"
        self.send(chat, self.t("anotar_saved", summary=what))
        self.announce(change, chat)

    def cmd_deshacer(self, msg: Mapping, rest: str, role: str) -> None:
        """/deshacer: the uncommitted batch, else the last `log:` commit (git does the work)."""
        chat = msg["chat"]["id"]
        try:
            change = self.repo.undo()
        except (GitError, OSError) as e:
            log.warning("undo failed: %s", safe(e))
            self.send(chat, self.t("toast_error"))
            return
        if not change:
            self.send(chat, self.t("undo_none"))
            return
        what = ", ".join(self.t.date(d) for d in change.dates)
        self.send(chat, self.t("undo_done", what=what))

    def cmd_hora(self, msg: Mapping, rest: str, role: str) -> None:
        """/hora HH:MM: the hour of the daily ask, with the same check as a `settings` block.
        No AI."""
        self.apply_settings(msg["chat"]["id"], {"ask_at": rest})

    def cmd_estado(self, msg: Mapping, rest: str, role: str) -> None:
        """/estado (admins): the phase, today's ask, the assistant and the git sync state."""
        self.send(msg["chat"]["id"], self.status_text(self.today()))

    def status_text(self, today: date) -> str:
        t = self.t
        log_ = self.repo.load()
        ph = self.phase(log_, today)
        p = ph.prediction
        dates = {}
        if p is not None:
            dates = {"ws": t.date(p.window_start, "short"), "we": t.date(p.window_end, "short")}
        phase = t(f"phase_{ph.kind}", n=ph.day, **dates)
        if self.kv.get("last_ask") != today.isoformat():
            asked = t("status_not_asked")
        elif today in log_ and log_[today].flow is not None:
            asked = t("status_answered")
        else:
            asked = t("status_asked")
        if not self.has_assistant:
            ai = t("status_ai_none")
        elif self.last_ai is None:
            ai = t("status_ai_never")
        else:
            ai = t("status_ai_ok", ago=_ago(self.now() - self.last_ai))
        unpushed = self.repo.unpushed()
        if unpushed:
            git = t("status_git_unpushed", n=unpushed)
        elif self.repo.remote_ok is False:
            git = t("status_git_down")
        elif unpushed is None:  # no upstream to count against: not known to be synced
            git = t("status_git_unknown")
        else:
            git = t("status_git_ok")
        return t("status", phase=phase, asked=asked, ask_at=self.profile.ask_at, ai=ai, git=git)

    def cmd_nuevo(self, msg: Mapping, rest: str, role: str) -> None:
        """/nuevo (admins): drop the pending lines and the assistant conversation, always, so
        an admin can always reset a chat. The plugin's `forget` is best effort: a broken id it
        refuses must not keep the chat stuck on it. A turn already running does not store its
        conversation afterwards."""
        chat = msg["chat"]["id"]
        with self._conv_lock:
            self._resets[chat] += 1
            conversation = self.kv.get(f"conv:{chat}")
            self.kv.delete(f"conv:{chat}")
        self.kv.delete(f"pending:{chat}")
        self.kv.delete(f"said:{chat}")
        self.kv.delete(f"options:{chat}")
        self.kv.delete(f"ctx:{chat}")
        self.kv.chat_clear(chat)
        if conversation and self.has_assistant:
            block = self.profile.assistant or {}
            try:
                assistant.forget(
                    self.data, block.get("plugin"), conversation, assistant.settings(block)
                )
            except Exception as e:  # noqa: BLE001 - the plugin may raise anything
                log.warning("forget failed: %s", safe(e))
        self.send(chat, self.t("new_conversation"))

    def cmd_presentacion(self, msg: Mapping, rest: str, role: str) -> None:
        """/presentacion (admins): the introduction, posted when an admin asks for it."""
        self.send(msg["chat"]["id"], self.intro_text())

    def intro_text(self) -> str:
        """The introduction (/presentacion and `sct say --intro`)."""
        return self.t("intro", name=html.escape(self.profile.name))

    def cmd_ayuda(self, msg: Mapping, rest: str, role: str) -> None:
        text = self.t("help")
        if role == ADMIN:
            text += "\n\n" + self.t("help_admin")
        self.send(msg["chat"]["id"], text)

    # --- the loop ------------------------------------------------------------------------

    def migrate(self, msg: Mapping) -> None:
        """A basic group became a supergroup: use the new id from now on."""
        old = (msg.get("chat") or {}).get("id")
        new = _int(msg.get("migrate_to_chat_id"))
        if new is None or old != self.chat_id:
            return
        self.kv.set("migrated_chat", str(new))
        log.warning("the group became a supergroup: update telegram.chat_id in profile.yaml")

    def ask_due(self, now: datetime) -> date | None:
        """The bot day whose scheduled card is due at `now`, or None. With `ask_dt =
        combine(T, ask_at)` in the profile timezone the card goes in `[ask_dt, ask_dt + 2 h)`,
        once per bot day, and only when the phase wants one."""
        today = cycle.bot_day(now, self.profile.timezone)
        if self.kv.get("last_ask") == today.isoformat():
            return None
        zone = ZoneInfo(self.profile.timezone)
        ask_dt = datetime.combine(today, ask_time(self.profile.ask_at), tzinfo=zone)
        if not ask_dt <= now < ask_dt + ASK_WINDOW:
            return None
        log_ = self.repo.load()
        ph = self.phase(log_, today)
        if ph.kind == "period":
            return today if self.queue(ph, log_, today) else None
        if ph.kind in ("waiting", "late"):
            day = log_.get(today)
            return today if day is None or day.flow is None else None
        return None

    def scheduled_ask(self, now: datetime) -> bool:
        chat = self.chat_id
        today = self.ask_due(now)
        if chat is None or today is None:
            return False
        try:
            self.send_card(chat, today)
        except TelegramError as e:
            if e.migrate_to_chat_id is not None:
                self.kv.set("migrated_chat", str(e.migrate_to_chat_id))
                log.warning("the group became a supergroup: update telegram.chat_id")
            log.warning("ask failed: %s", safe(e))
            return False
        self.kv.set("last_ask", today.isoformat())
        return True

    def tick(self, now: datetime | None = None) -> None:
        """Between two polls: the scheduled ask, the git sync and the heartbeat."""
        now = now or self.now()
        try:
            self.scheduled_ask(now)
        except Exception as e:  # noqa: BLE001 - the loop must go on
            log.warning("ask failed: %s", safe(e))
        try:
            self.repo.flush()
        except Exception as e:  # noqa: BLE001
            log.warning("git sync failed: %s", safe(e))
        if self.repo.profile_pulled:
            self.repo.profile_pulled = False
            self.reload_profile()
        try:
            self.expire_asks()
        except Exception as e:  # noqa: BLE001
            log.warning("ask expiry failed: %s", safe(e))
        for step, what in (
            (self.tick_reminders, "reminder tick"),
            (self.expire_offers, "offer expiry"),
            (self.expire_options, "options expiry"),
            (lambda: self.offer_plan(now), "plan offer"),
            (lambda: self.prune_log(now), "log prune"),
        ):
            try:
                step()
            except Exception as e:  # noqa: BLE001 - the loop must go on
                log.warning("%s failed: %s", what, safe(e))
        self.heartbeat()

    def prune_log(self, now: datetime) -> None:
        """Once an hour, the chat log forgets what is older than it needs: `history_days`, and
        at least the 7 days a turn's context lines reach back."""
        hour = int(now.timestamp()) // 3600
        if getattr(self, "_pruned", None) == hour:
            return
        self._pruned = hour
        days = max(self.profile.history_days or 0, LOG_KEEP_DAYS)
        gone = self.kv.chat_prune(int(now.timestamp()) - days * 86400)
        if gone:
            log.info("chat log pruned rows=%d", gone)

    def heartbeat(self) -> None:
        if self.alive is not None:
            try:
                self.alive.touch()
            except OSError as e:
                log.warning("heartbeat failed: %s", safe(e))

    @staticmethod
    def wait(stop: threading.Event, seconds: float, between: Callable[[], None]) -> None:
        """Wait `seconds` or until `stop`, calling `between` every `TICK_S` on the way: a
        backoff of up to 300 s must not stop the heartbeat, the scheduled ask or the git sync
        while only Telegram is down."""
        while seconds > 0 and not stop.wait(min(seconds, TICK_S)):
            seconds -= TICK_S
            if seconds > 0:
                between()

    def poll_once(self) -> float:
        """One `getUpdates` and its updates; returns how long to wait before the next one.
        409 (another poller) waits 30 s and keeps the offset; 429 waits what Telegram says;
        5xx and network failures back off from 5 s to 300 s; 401 raises."""
        offset = self.kv.get("offset")
        self.polling = True
        try:
            updates = self.tg.call(
                "getUpdates",
                offset=int(offset) if offset else None,
                timeout=POLL_S,
                allowed_updates=["message", "edited_message", "callback_query"],
            )
        except TelegramError as e:
            log.warning("getUpdates failed: %s", safe(e))
            if e.code == 401:
                raise
            if e.code == 409:
                return WAIT_409_S
            if e.code == 429:
                return float(min(e.retry_after or BACKOFF_MIN_S, BACKOFF_MAX_S))
            delay = self._backoff
            self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
            return delay
        finally:
            self.polling = False
        self._backoff = BACKOFF_MIN_S
        for update in updates or []:
            try:
                self.handle(update)
            except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                log.error("update failed: %s", safe(e))
            self.kv.set("offset", str(int(update["update_id"]) + 1))
        return 0

    def run(self, stop: threading.Event) -> None:
        """The loop: getMe, the menu and the remote check once, then poll and tick until `stop`
        is set. On the way out (SIGTERM) the log is committed and pushed at once."""
        delay = BACKOFF_MIN_S
        while not stop.is_set():
            try:
                self.start()
                break
            except TelegramError as e:
                if e.code == 401:
                    raise
                log.warning("getMe failed: %s", safe(e))
                self.wait(stop, delay, self.heartbeat)
                delay = min(delay * 2, BACKOFF_MAX_S)
        self.repo.check_remote()
        self.set_commands()
        self.resume()
        try:
            while not stop.is_set():
                try:
                    wait = self.poll_once()
                except Interrupted:
                    break
                self.tick()
                if wait:
                    self.wait(stop, wait, self.tick)
        finally:
            self.repo.flush(force=True)
            log.info("stopped")

    # --- the assistant turn (worker thread) ----------------------------------------------

    def settle(self, chat: int) -> None:
        """Wait until the chat's newest line is `debounce_s` old and no voice note of it is still
        being heard, for `voice_wait_s` at most from that note's arrival."""
        while True:
            with self._q:
                newest = self._newest.get(chat)
                notes = list((self._voicing.get(chat) or {}).values())
            now = self.clock()
            wait = newest + self.debounce_s - now if newest is not None else 0.0
            if notes:
                wait = max(wait, min(notes) + self.voice_wait_s - now)
            if wait <= 0:
                return
            self.sleep(min(wait, 0.5))

    def _turn(self, chat: int) -> None:
        """One turn: after the debounce, every pending text line of the chat goes as `new`, and
        what arrives while it runs is handed over through `incoming`. On success the text is
        posted, the block applied and the lines it covered removed (the rest stay for the next
        turn). On an exception, a timeout or `kept: false`: «ai_down» with the keyboard, no
        block, and every line stays for the next turn."""
        key = f"pending:{chat}"
        self.settle(chat)
        with self._q:
            lines = [x for x in self.kv.get_json(key, []) or [] if "content" in x]
            if not lines:
                return
            state = Turn(chat, [x["id"] for x in lines], _users(lines), _mid(lines[-1]))
            state.grants = [_grant(x["grant"]) for x in lines if isinstance(x.get("grant"), dict)]
            self._running[chat] = state
        try:
            self._converse(chat, key, lines, state)
        finally:
            with self._q:
                state.closed = True
                self._running.pop(chat, None)
            self.close_asks(state)

    def _converse(self, chat: int, key: str, lines: list[dict], state: Turn) -> None:
        p = self.profile
        block_cfg = p.assistant or {}
        try:
            self.tg.call("sendChatAction", chat_id=chat, action="typing")
        except TelegramError as e:
            log.warning("sendChatAction failed: %s", safe(e))
        started = time.monotonic()
        rows = {x.get("row") for x in lines}  # a line held for the next turn is not history
        lines, atts = self.fetch_files(lines, chat)
        state.ids = [x["id"] for x in lines]
        now = self.now()
        today = cycle.bot_day(now, p.timezone)
        with self._conv_lock:
            resets = self._resets[chat]
            conversation = self.kv.get(f"conv:{chat}")
        now_ts = int(now.timestamp())
        last_row = self.kv.chat_last(chat)
        seen = self.context_rows(chat, now_ts, last_row)
        try:
            with DATA_LOCK:
                try:
                    log_text = self.repo.log_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    log_text = ""
            log_ = records.loads(log_text)
            admins = list(p.admins.values())
            plan = self.plan(log_, today)[0]
            context = assistant.context(
                log_,
                now,
                tz=p.timezone,
                t=self.t,
                name=p.name,
                admins=admins,
                birth_date=p.birth_date,
                ask_at=p.ask_at,
                period_max_day=self.max_day,
                asked=self.kv.get("last_ask") == today.isoformat(),
                skipped=self.skipped(),
                plan=plan,
            )
            rules = assistant.rules(
                p.name, p.language, admins, block_cfg.get("instructions") or "", self.read_all
            )
            turn = assistant.turn(
                conversation,
                rules,
                context,
                log_text,
                self.new_lines(lines, seen),
                self.history(chat, now_ts, last_row, rows | {r["id"] for r in seen}),
                attachments=atts,
            )
            turn["incoming"] = self._incoming(state, key)
            if assistant.asks(self.data, block_cfg.get("plugin")):
                turn["ask"] = self._asker(state)
            reply = assistant.converse(
                self.data, block_cfg.get("plugin"), turn, assistant.settings(block_cfg)
            )
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("ai turn failed: %s", safe(e))
            self.post(chat, self.t("ai_down"), self.today_keyboard())
            return
        finally:
            with self._q:
                state.closed = True  # a plugin thread that outlives the turn takes nothing more
        if not reply.kept:
            log.warning("ai turn not kept")
            self.post(chat, self.t("ai_down"), self.today_keyboard())
            return
        log.info("ai turn ok %ds", round(time.monotonic() - started))
        self.last_ai = self.now()
        with self._conv_lock:
            if self._resets[chat] != resets:
                log.info("conversation dropped by /nuevo during the turn")
            elif reply.conversation:
                self.kv.set(f"conv:{chat}", reply.conversation)
            else:
                self.kv.delete(f"conv:{chat}")
        # what the answer covers: the lines it started with, and the handed-over lines it names
        handed = [
            x
            for x in self.kv.get_json(key, []) or []
            if x.get("id") in state.pulled and x.get("id") in reply.covers
        ]
        covered = [*lines, *handed]
        reply_to = _mid(covered[0]) if len(covered) == 1 else None
        text, block = assistant.extract(reply.text)
        text, settings = assistant.extract(text, "settings")
        text, offered = assistant.extract_options(text)
        text, listed = assistant.extract_reminders(text)
        offered = reply.options or offered
        listed = reply.reminders or listed
        users = _users(covered)
        if text and not self.say(chat, text, reply_to, offered, users):
            # she never saw the answer: a failed turn, so the buttons and the lines stay
            self.post(chat, self.t("ai_down"), self.today_keyboard())
            return
        if text:
            self.kv.add_chat(chat, "assistant", None, text, int(self.now().timestamp()))
        if block is not None:
            source = "admin" if all(x.get("admin") for x in covered) else "telegram"
            self.apply_block(chat, block, source, today)
        if settings is not None:
            self.apply_settings(chat, settings)
        if text:
            self.remember(chat, covered, text)
        # last, so a crash before here sends the lines again; a block applied twice changes
        # nothing the second time
        taken = {x["id"] for x in covered}
        self.kv.update_json(key, lambda cur: [x for x in cur or [] if x["id"] not in taken] or None)
        if seen:  # the context lines went with a turn that was kept: not again
            self.kv.set(f"ctx:{chat}", str(max(r["id"] for r in seen)))
        if listed:
            self.offer_reminders(chat, listed, covered, reply_to)
        missed = len(state.pulled) - len(handed)
        if handed or missed:
            log.info("handed over: covered=%d next_turn=%d", len(handed), missed)
        if reply.later is not None:
            job = reply.later
            self.later.submit(lambda: self.run_later(chat, job))
            log.info("later queued")

    def fetch_files(self, lines: list[dict], chat: int) -> tuple[list[dict], list[dict]]:
        """Download the photos and files of the turn's lines, 20 MiB each and 25 MiB in all, as
        the plugin's attachments `{"name", "mime", "data", "from"}`. A line past the budget
        waits for the next turn, which is queued; a file that cannot be fetched says so under its
        message («file_too_big» or «file_failed») and its line goes on without it, saying so."""
        kept, atts, total, held = [], [], 0, False
        for x in lines:
            f = x.get("file")
            if not isinstance(f, Mapping):
                kept.append(x)
                continue
            size = f.get("size") if isinstance(f.get("size"), int) else 0
            if atts and total + size > TURN_FILES_MAX:
                held = True
                continue
            try:
                info = self.tg.call("getFile", file_id=f["file_id"])
                info = info if isinstance(info, Mapping) else {}
                path = info.get("file_path")
                if isinstance(info.get("file_size"), int) and info["file_size"] > FILE_MAX:
                    raise TelegramError("getFile", 413, "file too large")
                if not isinstance(path, str) or not path:
                    raise TelegramError("getFile", 0, "no file path")
                data = self.tg.download(path, FILE_MAX)
            except TelegramError as e:
                log.warning("file not fetched: %s", safe(e))
                too_big = e.code == 413 or "too big" in e.description.lower()
                note = {"message_id": _mid(x), "allow_sending_without_reply": True}
                self.post(
                    chat,
                    self.t("file_too_big" if too_big else "file_failed"),
                    reply_parameters=note,
                )
                gone = x["content"].replace(f": {f['name']}]", " not downloaded]", 1)
                kept.append({**x, "file": None, "content": gone})
                continue
            if atts and total + len(data) > TURN_FILES_MAX:
                held = True
                continue
            total += len(data)
            role = self.role(x.get("uid"))
            who = self.who(role, x.get("uid")) if role else None
            atts.append({"name": f["name"], "mime": f["mime"], "data": data, "from": who})
            kept.append(x)
        if held:
            self.worker.submit(chat)
        return kept, atts

    def _incoming(self, state: Turn, key: str) -> Callable[[], list[dict]]:
        """`turn["incoming"]`: the chat's text lines queued since the turn started and not handed
        over yet, for the plugin's thread. A line with a photo or a file waits for the next turn,
        which carries the file. Once the turn is over it hands over nothing, so a late call steals
        nothing from the next turn."""

        def incoming() -> list[dict]:
            with self._q:
                if state.closed:
                    return []
                got = [
                    x
                    for x in self.kv.get_json(key, []) or []
                    if "content" in x and not x.get("file") and not state.has(x.get("id"))
                ]
                state.pulled.extend(x["id"] for x in got)
                state.grants += [
                    _grant(x["grant"]) for x in got if isinstance(x.get("grant"), dict)
                ]
                for uid in _users(got):
                    if uid not in state.users:
                        state.users.append(uid)
            return [
                {
                    "id": x["id"],
                    "role": "user",
                    "content": x["content"],
                    "ts": datetime.fromtimestamp(int(x.get("ts") or 0), UTC).isoformat(),
                }
                for x in got
            ]

        return incoming

    def context_rows(self, chat: int, now: int, upto: int) -> list[dict]:
        """The group lines that did not call the bot and no kept turn has taken yet (the
        newest CONTEXT_MAX, none older than 7 days): context for this turn."""
        after = int(self.kv.get(f"ctx:{chat}") or 0)
        rows = self.kv.chat_rows(chat, now - PENDING_MAX_AGE_S, upto)
        return [r for r in rows if not r["asked"] and r["role"] == "user" and r["id"] > after][
            -CONTEXT_MAX:
        ]

    @staticmethod
    def new_lines(lines: Sequence[Mapping], seen: Sequence[Mapping]) -> list[dict]:
        """The turn's `new`: the lines for the bot and the group lines it only saw, marked
        «[not to you]», in the order they came."""
        items = [(int(r["ts"]), 0, i, NOT_TO_YOU + r["text"]) for i, r in enumerate(seen)]
        items += [(int(x.get("ts") or 0), 1, i, x["content"]) for i, x in enumerate(lines)]
        return [{"content": c, "ts": _iso(ts)} for ts, _, _, c in sorted(items)]

    def history(self, chat: int, now: int, upto: int, skip: set) -> list[dict]:
        """The chat log of the last `history_days` before this turn, without the lines the turn
        sends as new: the recap a provider starts a conversation over from."""
        days = self.profile.history_days or 0
        if days <= 0:
            return []
        out = []
        for r in self.kv.chat_rows(chat, now - days * 86400, upto):
            if r["id"] in skip:
                continue
            text = r["text"] if r["role"] == "assistant" or r["asked"] else NOT_TO_YOU + r["text"]
            role = "assistant" if r["role"] == "assistant" else "user"
            out.append({"role": role, "content": text, "ts": _iso(r["ts"])})
        return out

    def remember(self, chat: int, covered: Sequence[Mapping], answer: str) -> None:
        """Keep the words of the messages an answer covered, with that answer, so an edit of one
        of them can say what changed (`said:<chat>`, the newest SAID_MAX, none older than 7
        days)."""
        now = int(self.now().timestamp())
        said = {
            str(_mid(x)): {"body": x["body"], "answer": answer[:ANSWER_CLIP], "ts": now}
            for x in covered
            if _mid(x) is not None and isinstance(x.get("body"), str) and not x.get("tap")
        }
        if not said:
            return

        def add(cur):
            merged = {**(cur or {}), **said}
            fresh = [
                kv for kv in merged.items() if now - int(kv[1].get("ts", 0)) <= PENDING_MAX_AGE_S
            ]
            fresh.sort(key=lambda kv: kv[1].get("ts", 0))
            return dict(fresh[-SAID_MAX:]) or None

        self.kv.update_json(f"said:{chat}", add, {})

    def run_later(self, chat: int, job: Callable[[], str]) -> None:
        """A plugin's `later` (on the later thread): its string goes to the chat as plain text,
        nothing when it is empty; a failure posts «later_failed» and logs the class only."""
        started = time.monotonic()
        try:
            out = job()
            if not isinstance(out, str):
                raise assistant.PluginError("bad_later")
        except BaseException as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("later failed: %s", safe(e))
            self.post(chat, self.t("later_failed"))
            return
        log.info("later done %ds", round(time.monotonic() - started))
        if out.strip() and self.say(chat, out.strip(), None):
            self.kv.add_chat(chat, "assistant", None, out.strip(), int(self.now().timestamp()))

    # --- asks: an action the assistant wants to take, approved by a person ----------------

    def _asker(self, state: Turn) -> Callable[[object], bool]:
        """`turn["ask"]`: the plugin's thread asks a person to approve an action."""

        def ask(request: object) -> bool:
            return self.pose(state, request)

        return ask

    def approvers(self, state: Turn, confirm_by: object) -> list[int]:
        """Who may answer an ask: the person the turn answers, when it is asked of them and that
        person is her or an admin; else the admins (also when the turn carries several people's
        messages)."""
        users = list(state.users)
        if confirm_by == "asker" and len(users) == 1 and self.role(users[0]) is not None:
            return users
        return list(self.profile.admins)

    def names(self, users: Sequence[int]) -> str:
        out = [
            self.mention()
            if u == self.profile.user_id
            else html.escape(self.profile.admins.get(u, ""))
            for u in users
        ]
        return ", ".join(x for x in out if x)

    def pose(self, state: Turn, request: object) -> bool:
        """Post one of the plugin's asks with Approve and Reject (on the plugin's thread). True
        once it is settled: posted, or declined at once because nobody can answer it or Telegram
        refused it. False when Telegram could not be reached: the plugin asks again later."""
        if not isinstance(request, Mapping) or not isinstance(request.get("id"), str):
            return True
        request = dict(request)
        summary = str(request.get("summary") or "").strip()
        with self._q:
            if any(a["request"].get("id") == request["id"] for a in self.asks.values()):
                return True
        expires = request.get("expires_at")
        ok_end = isinstance(expires, int | float) and not isinstance(expires, bool)
        expires = float(expires) if ok_end else time.time() + ASK_DEFAULT_S
        if not state.closed and summary and expires > time.time():
            grant = self.granted(state, request)
            if grant is not None:
                self.preapproved(state, request, grant, summary)
                return True
        by = self.approvers(state, request.get("confirm_by"))
        if state.closed or not by or not summary or expires <= time.time():
            self.decline(request, NO_ONE)
            log.info("ask declined: nobody to ask")
            return True
        key = secrets.token_hex(4)
        text = self.t("ask_confirm", name=self.names(by), summary=html.escape(summary, quote=False))
        markup = _keyboard(
            [[(self.t("button_approve"), f"a:{key}:y"), (self.t("button_reject"), f"a:{key}:n")]]
        )
        where = (
            {"message_id": state.mid, "allow_sending_without_reply": True} if state.mid else None
        )
        try:
            sent = self.send(state.chat, text, markup, reply_parameters=where)
        except TelegramError as e:
            log.warning("ask not posted: %s", safe(e))
            if e.code == 0 or e.code == 429 or e.code >= 500:
                return False  # Telegram was not reached: kept, asked again on the next look
            self.decline(request, NO_ONE)
            return True
        item = {
            "request": request,
            "by": by,
            "chat": state.chat,
            "msg": (sent or {}).get("message_id"),
            "expires": expires,
        }
        with self._q:
            self.asks[key] = item
            state.asks.append(key)
            late = state.closed
        if late:  # the turn ended while it was posted
            self.close_asks(state)
        log.info("ask posted")
        return True

    def granted(self, state: Turn, request: Mapping) -> dict | None:
        """The grant a calendar button gave this turn for this ask, used up, or None.

        A reminder's «En el calendario» names one time: the turn's first ask uses it up, and it
        approves that ask only when the ask adds an event at that time (`action` "add", `when`
        within GRANT_SLACK_S). The plan's button names its entries: an ask that adds one of
        them exactly (`event` with the same title, start and end) is approved, each once."""
        if request.get("action") != "add":
            return None
        when, event = request.get("when"), request.get("event")
        with self._q:
            for g in state.grants:
                if "at" in g:
                    if g.get("used"):
                        continue
                    g["used"] = True
                    ok = isinstance(when, int | float) and not isinstance(when, bool)
                    return g if ok and abs(float(when) - float(g["at"])) < GRANT_SLACK_S else None
                for e in g.get("events") or []:
                    if not e.get("used") and _same_event(event, e):
                        e["used"] = True
                        return g
        return None

    def preapproved(self, state: Turn, request: Mapping, grant: Mapping, summary: str) -> None:
        """An ask a calendar button already approved: answered yes for the person who tapped,
        and the chat sees what was approved and how it went."""
        block = self.profile.assistant or {}
        try:
            out = assistant.answer(
                self.data,
                block.get("plugin"),
                dict(request),
                {"approved": True, "by": str(grant.get("by")), "note": "approved by the button"},
                assistant.settings(block),
            )
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("a preapproved ask failed: %s", safe(e))
            out = {"ok": False, "error": None}
        if out.get("ok"):
            said = self.t("ask_done")
        elif out.get("error"):
            said = self.t("ask_failed", error=html.escape(str(out["error"])[:300], quote=False))
        else:
            said = self.t("ask_failed_unknown")
        what = html.escape(summary, quote=False)
        event = request.get("event")
        if isinstance(event, Mapping) and event.get("title"):
            what = self.entry_text(str(event["title"]), event.get("start"), event.get("end"))
        where = (
            {"message_id": state.mid, "allow_sending_without_reply": True} if state.mid else None
        )
        self.post(
            state.chat, self.t("ask_preapproved", what=what, result=said), reply_parameters=where
        )
        log.info("ask approved by a button")

    def decline(self, request: Mapping, note: str, by: str = "bot") -> None:
        block = self.profile.assistant or {}
        try:
            assistant.answer(
                self.data,
                block.get("plugin"),
                dict(request),
                {"approved": False, "by": by, "note": note},
                assistant.settings(block),
            )
        except Exception as e:  # noqa: BLE001 - the ask expires on the provider's side anyway
            log.warning("could not decline an ask: %s", safe(e))

    def tapped_ask(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        """Approve or Reject: only from the people the ask names, once, before it expires. The
        answer goes back to the plugin off the poll loop; after an approval the chat hears
        whether it went through."""
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        with self._q:
            item = self.asks.get(key)
            if item is not None and item["expires"] <= time.time():
                self.asks.pop(key, None)
                item = None
            mine = item is not None and user in item["by"]
            if mine:
                self.asks.pop(key, None)  # answered once
        if item is None:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if not mine:
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(item["by"])))
            return
        yes = choice == "y"
        self.answer(cq["id"])
        self.unbutton(
            chat, item["msg"], "✓ " + self.t("button_approve" if yes else "button_reject")
        )
        self.side.submit(lambda: self.settle_ask(item, yes, user))
        log.info("ask answered")

    def plain_names(self, users: Sequence[int]) -> str:
        """Names for a toast, which shows no HTML."""
        out = [self.who(self.role(u) or "", u) or "" for u in users]
        return ", ".join(x for x in out if x)

    def settle_ask(self, item: Mapping, yes: bool, user: object) -> None:
        """The tap, back to the plugin (on the asks thread). After an approval the chat hears
        «Hecho» or why it failed; after a rejection the turn's answer says what came of it."""
        block = self.profile.assistant or {}
        try:
            out = assistant.answer(
                self.data,
                block.get("plugin"),
                dict(item["request"]),
                {"approved": yes, "by": str(user), "note": None},
                assistant.settings(block),
            )
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("an ask's answer failed: %s", safe(e))
            out = {"ok": False, "error": None}
        if not yes:
            return
        if out.get("ok"):
            said = self.t("ask_done")
        elif out.get("error"):
            said = self.t("ask_failed", error=html.escape(str(out["error"])[:300], quote=False))
        else:
            said = self.t("ask_failed_unknown")
        where = {"message_id": item["msg"], "allow_sending_without_reply": True}
        self.post(item["chat"], said, reply_parameters=where if item["msg"] else None)

    def close_asks(self, state: Turn, note: str | None = None) -> None:
        """The turn ended, or a message of it was edited: its open asks lose their buttons. With
        a note, each is declined with it first and the chat hears why."""
        with self._q:
            keys, state.asks[:] = list(state.asks), []
            items = [self.asks.pop(k) for k in keys if k in self.asks]
        for item in items:
            if note:
                self.decline(item["request"], note)
            self.unbutton(item["chat"], item["msg"])
            if note:
                where = {"message_id": item["msg"], "allow_sending_without_reply": True}
                self.post(item["chat"], self.t("ask_edited"), reply_parameters=where)

    def expire_asks(self) -> None:
        """Past its end an ask loses its buttons; the plugin's side has already heard it."""
        now = time.time()
        with self._q:
            gone = [k for k, v in self.asks.items() if v["expires"] <= now]
            items = [self.asks.pop(k) for k in gone]
        for item in items:
            self.unbutton(item["chat"], item["msg"])

    def unbutton(self, chat: int, message_id: object, label: str | None = None) -> None:
        """Take the buttons off a message (best effort); `label`: one inert button that says what
        was chosen instead."""
        if not message_id:
            return
        markup = _keyboard([[(label, "x")]]) if label else {"inline_keyboard": []}
        try:
            self.tg.call(
                "editMessageReplyMarkup", chat_id=chat, message_id=message_id, reply_markup=markup
            )
        except TelegramError as e:
            log.info("buttons not removed: %s", safe(e))

    # --- choices under an answer ---------------------------------------------------------

    def yes_no(self, labels: Sequence[str]) -> bool:
        return sorted(fold(x) for x in labels) == sorted(
            fold(self.t(k)) for k in ("button_yes", "button_no")
        )

    def offer(self, chat: int, msg: object, key: str, labels: Sequence[str], users) -> None:
        now = int(self.now().timestamp())
        item = {"key": key, "msg": msg, "labels": list(labels), "for": list(users), "ts": now}

        def add(cur):
            kept = [o for o in cur or [] if now - int(o.get("ts", 0)) <= OPTIONS_TTL_S]
            return [*kept, item][-OPTIONS_KEEP:]

        self.kv.update_json(f"options:{chat}", add, [])

    def tapped_option(self, cq: Mapping, chat: int, data: str, user: object, role: str) -> None:
        """A choice under an answer: that person's next message, as if typed. The buttons give
        way to the one chosen; only the people the answer was for may tap, once."""
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        now = int(self.now().timestamp())
        offers = self.kv.get_json(f"options:{chat}", []) or []
        item = next((o for o in offers if o.get("key") == key), None)
        if item is None or now - int(item.get("ts", 0)) > OPTIONS_TTL_S:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if item.get("for") and user not in item["for"]:
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(item["for"])))
            return
        labels = item.get("labels") or []
        if not choice.isdigit() or int(choice) >= len(labels):
            self.answer(cq["id"])
            return
        label = labels[int(choice)]
        self.kv.update_json(
            f"options:{chat}", lambda cur: [o for o in cur or [] if o.get("key") != key] or None
        )
        self.answer(cq["id"])
        self.unbutton(chat, mid, f"✓ {label}")
        content = f"{self.who(role, user)}: {label}"
        line = {
            "id": f"o{key}",
            "mid": mid,
            "ts": now,
            "content": content,
            "admin": role == ADMIN,
            "uid": user,
            "body": label,
            "tap": True,
            "row": self.kv.add_chat(chat, "user", self.who(role, user), content, now),
        }
        self.queue_line(chat, line)
        log.info("option tapped")

    def expire_options(self) -> None:
        """Choices nobody tapped in OPTIONS_TTL_S: nothing is done, and their buttons say so."""
        now = int(self.now().timestamp())
        for key in self.kv.keys("options:"):
            gone: list[dict] = []

            def keep(cur, gone=gone):
                fresh = []
                for o in cur or []:
                    (gone if now - int(o.get("ts", 0)) > OPTIONS_TTL_S else fresh).append(o)
                return fresh or None

            self.kv.update_json(key, keep, [])
            for o in gone:
                self.unbutton(int(key.removeprefix("options:")), o.get("msg"), self.t(OFFER_GONE))

    # --- reminders and the calendar plan, offered with buttons ---------------------------

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.profile.timezone)

    def when_text(self, at: float) -> str:
        """A reminder's time as she reads it: «domingo 18 de octubre, 18:00»."""
        local = datetime.fromtimestamp(at, self.zone())
        return self.t("reminder_when", date=self.t.date(local.date()), time=f"{local:%H:%M}")

    def entry_text(self, title: str, start: object, end: object) -> str:
        """A plan entry as she reads it: «Fase lútea: del 11 al 23 de octubre» (the end is
        exclusive, so the last day shown is the day before it)."""
        try:
            first = date.fromisoformat(str(start))
            last = date.fromisoformat(str(end)) - timedelta(days=1)
        except ValueError:
            return html.escape(title)
        if last <= first:
            return self.t("entry_day", title=html.escape(title), day=self.t.date(first, "short"))
        return self.t(
            "entry_days",
            title=html.escape(title),
            first=self.t.date(first, "short"),
            last=self.t.date(last, "short"),
        )

    def mention_of(self, uid: object) -> str:
        if uid == self.profile.user_id:
            return self.mention()
        name = html.escape(self.profile.admins.get(uid, "") or "")
        return f'<a href="tg://user?id={uid}">{name}</a>' if name else ""

    def offers(self, chat: int) -> list[dict]:
        return [o for o in self.kv.get_json(f"remind:{chat}", []) or [] if isinstance(o, dict)]

    def keep_offer(self, chat: int, offer: dict) -> None:
        """Store an offer, or drop it once nothing in it waits for a tap."""
        key = offer["key"]
        waiting = any(not i.get("done") for i in offer.get("items") or [])

        def put(cur):
            rest = [o for o in cur or [] if o.get("key") != key]
            return [*rest, offer] if waiting else rest or None

        self.kv.update_json(f"remind:{chat}", put, [])

    def remind_rows(self, offer: Mapping) -> list:
        """Buttons for each offered reminder (here, in the calendar, nothing), or one inert
        button saying what became of it."""
        t, key, items = self.t, offer["key"], offer["items"]
        calendar = bool(self.profile.calendar.get("reminders"))
        rows = []
        for n, r in enumerate(items):
            pre = f"{n + 1} · " if len(items) > 1 else ""
            if r.get("done"):
                rows.append([(f"✓ {pre}{t('remind_' + r['done'])}", "x")])
                continue
            first = [(pre + t("remind_here"), f"r:{key}:{n}h")]
            if calendar:
                first.append((pre + t("remind_calendar"), f"r:{key}:{n}c"))
            rows += [first, [(pre + t("remind_nothing"), f"r:{key}:{n}x")]]
        return rows

    def plan_rows(self, offer: Mapping) -> list:
        done = offer["items"][0].get("done")
        if done:
            # a tap is an action, so the question's answers are Sí and No; «nada» is only no tap
            label = {"calendar": "plan_yes", "nothing": "plan_no"}.get(done, "remind_" + done)
            return [[("✓ " + self.t(label), "x")]]
        key = offer["key"]
        return [
            [(self.t("plan_yes"), f"c:{key}:y"), (self.t("plan_no"), f"c:{key}:n")],
        ]

    def rows_of(self, offer: Mapping) -> list:
        return self.plan_rows(offer) if offer.get("kind") == "plan" else self.remind_rows(offer)

    def rekey(self, chat: int, message_id: object, rows: list) -> None:
        if not message_id:
            return
        try:
            self.tg.call(
                "editMessageReplyMarkup",
                chat_id=chat,
                message_id=message_id,
                reply_markup=_keyboard(rows),
            )
        except TelegramError as e:
            log.info("buttons not changed: %s", safe(e))

    def offer_reminders(
        self, chat: int, listed: Sequence[Mapping], covered: Sequence[Mapping], reply_to
    ) -> None:
        """One message with the reminders an answer listed, buttons for each. Nothing happens
        until a tap; untapped, the offer expires (REMIND_TTL_S) and nothing is done. A reminder
        is for the person the answer was for (her, when it was for several)."""
        now = self.now().timestamp()
        items = []
        for r in listed:
            try:
                at = datetime.strptime(r["when"], "%Y-%m-%d %H:%M").replace(tzinfo=self.zone())
            except (KeyError, TypeError, ValueError):
                continue
            if now + 60 <= at.timestamp() <= now + REMIND_DAYS * 86400:
                items.append({"at": at.timestamp(), "text": r["text"], "done": None})
        if not items:
            return
        users = _users(covered)
        uid = users[0] if len(users) == 1 else self.profile.user_id
        many = len(items) > 1
        lines = "\n".join(
            (f"{n + 1}. " if many else "") + f"{self.when_text(r['at'])}: {html.escape(r['text'])}"
            for n, r in enumerate(items)
        )
        offer = {
            "key": secrets.token_hex(4),
            "kind": "remind",
            "uid": uid,
            "for": list(dict.fromkeys([*users, uid])),
            "items": items,
            "ts": now,
            "ttl": REMIND_TTL_S,
        }
        where = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
        name = self.mention_of(uid)
        text = self.t("remind_offer" if name else "remind_offer_nobody", name=name, lines=lines)
        sent = self.post(chat, text, _keyboard(self.remind_rows(offer)), reply_parameters=where)
        if sent is None:
            return
        offer["msg"] = sent.get("message_id")
        self.keep_offer(chat, offer)
        log.info("reminders offered n=%d", len(items))

    def tapped_remind(self, cq: Mapping, chat: int, data: str, user: object, role: str) -> None:
        """Where an offered reminder goes: here (stored, posted at its time), in the calendar (a
        turn in which the assistant adds it, approved by this tap) or nothing."""
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        offer = next((o for o in self.offers(chat) if o.get("key") == key), None)
        if offer is None or self.now().timestamp() - offer.get("ts", 0) > offer.get(
            "ttl", REMIND_TTL_S
        ):
            self.answer(cq["id"], self.t("tap_gone"))
            if offer is None:
                self.unbutton(chat, mid)
            return
        if user not in offer.get("for", []):
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(offer["for"])))
            return
        n, c = choice[:-1], choice[-1:]
        items = offer["items"]
        calendar = bool(self.profile.calendar.get("reminders"))
        if (
            not n.isdigit()
            or int(n) >= len(items)
            or c not in ("h", "c", "x")
            or (c == "c" and not calendar)
        ):
            self.answer(cq["id"])
            return
        item = items[int(n)]
        if item.get("done"):
            self.answer(cq["id"], self.t("tap_gone"))
            return
        item["done"] = {"h": "here", "c": "calendar", "x": "nothing"}[c]
        self.keep_offer(chat, offer)  # before anything else: a second tap finds it settled
        when = self.when_text(item["at"])
        self.answer(cq["id"], self.t("remind_set", when=when) if c == "h" else None)
        self.rekey(chat, offer.get("msg"), self.remind_rows(offer))
        now = int(self.now().timestamp())
        if c == "h":
            self.kv.add_reminder(chat, offer.get("uid"), item["at"], item["text"], user, now)
            said = self.t("remind_set_log", when=when, text=item["text"])
            self.kv.add_chat(chat, "assistant", None, said, now)
            log.info("reminder set")
        elif c == "c":
            local = datetime.fromtimestamp(item["at"], self.zone())
            asked = self.t(
                "remind_calendar_turn", when=when, iso=local.isoformat(), text=item["text"]
            )
            self.queue_tap(chat, mid, user, role, asked, {"by": user, "at": item["at"]})
            log.info("reminder to the calendar")

    def queue_tap(
        self, chat: int, mid: object, user: object, role: str, text: str, grant: dict
    ) -> None:
        """A calendar button's tap as that person's message, carrying what it approves."""
        now = int(self.now().timestamp())
        content = f"{self.who(role, user)}: {text}"
        self.queue_line(
            chat,
            {
                "id": f"g{secrets.token_hex(4)}",
                "mid": mid,
                "ts": now,
                "content": content,
                "admin": role == ADMIN,
                "uid": user,
                "tap": True,
                "grant": grant,
                "row": self.kv.add_chat(chat, "user", self.who(role, user), content, now),
            },
        )

    def expire_offers(self) -> None:
        """Offers nobody settled in time: nothing is done, and their buttons say so."""
        now = self.now().timestamp()
        for key in self.kv.keys("remind:"):
            chat = int(key.removeprefix("remind:"))
            gone: list[dict] = []

            def keep(cur, gone=gone):
                fresh = []
                for o in cur or []:
                    late = now - o.get("ts", 0) > o.get("ttl", REMIND_TTL_S)
                    (gone if late else fresh).append(o)
                return fresh or None

            self.kv.update_json(key, keep, [])
            for o in gone:
                for r in o.get("items") or []:
                    r["done"] = r.get("done") or "timeout"
                self.rekey(chat, o.get("msg"), self.rows_of(o))

    def tick_reminders(self) -> None:
        """Post every reminder whose time has come, naming the person it is for, then mark it
        sent. One Telegram refuses for good (the chat is gone) is marked failed; one it could
        not take now is tried again on the next tick."""
        now = self.now().timestamp()
        for r in self.kv.due_reminders(now):
            name = self.mention_of(r["uid"])
            text = (
                f"⏰ {name}: {html.escape(r['text'])}" if name else f"⏰ {html.escape(r['text'])}"
            )
            if now - r["at"] > LATE_S:
                text += "\n" + self.t("reminder_late", when=self.when_text(r["at"]))
            try:
                self.send(r["chat"], text)
            except TelegramError as e:
                log.warning("reminder not posted: %s", safe(e))
                if e.code in (400, 403):
                    self.kv.settle_reminder(r["id"], "failed")
                continue
            self.kv.settle_reminder(r["id"], "sent")
            self.kv.add_chat(r["chat"], "assistant", None, text, int(self.now().timestamp()))
            log.info("reminder posted")

    def cmd_recordatorios(self, msg: Mapping, rest: str, role: str) -> None:
        """/recordatorios: the chat's reminders still to come, each with a button to cancel it."""
        chat = msg["chat"]["id"]
        rows = self.kv.reminders(chat)[:20]
        if not rows:
            self.send(chat, self.t("reminders_none"))
            return
        lines, buttons = [self.t("reminders_title")], []
        for n, r in enumerate(rows, 1):
            # plain names: listing a reminder must not notify the person it is for
            who = self.who(self.role(r["uid"]) or "", r["uid"])
            name = f" · {html.escape(who)}" if who else ""
            lines.append(f"{n}. {self.when_text(r['at'])}{name}: {html.escape(r['text'])}")
            buttons.append([(self.t("reminders_cancel", n=n), f"k:{r['id']}:{n}")])
        self.send(chat, "\n".join(lines), _keyboard(buttons))

    def tapped_cancel(self, cq: Mapping, chat: int, data: str) -> None:
        """Cancel a reminder from the /recordatorios list; its button says so."""
        _, rid, n = [*data.split(":"), "", ""][:3]
        message = cq.get("message") or {}
        found = [r for r in self.kv.reminders(chat) if str(r["id"]) == rid]
        if not found or not self.kv.settle_reminder(found[0]["id"], "cancelled"):
            self.answer(cq["id"], self.t("tap_gone"))
            return
        r = found[0]
        self.answer(cq["id"], self.t("reminder_cancelled"))
        said = self.t("reminder_cancelled_log", when=self.when_text(r["at"]), text=r["text"])
        self.kv.add_chat(chat, "assistant", None, said, int(self.now().timestamp()))
        rows = [
            [
                (f"✓ {self.t('reminders_cancelled', n=n)}", "x")
                if b.get("callback_data") == data
                else (b.get("text", ""), b.get("callback_data", "x"))
                for b in row
            ]
            for row in (message.get("reply_markup") or {}).get("inline_keyboard") or []
        ]
        self.rekey(chat, message.get("message_id"), rows)
        log.info("reminder cancelled")

    def plan(self, log_: Log, today: date) -> tuple[list[tuple[str, date, date]], str | None]:
        """The calendar plan with its titles, and a key that changes only when the prediction
        or the titles do; nothing without `calendar.events` in the profile."""
        cal = self.profile.calendar
        if not cal.get("events"):
            return [], None
        titles = {k: str(self.t(f"plan_{k}")) for k in cycle.PLAN_KINDS}
        for k, v in (cal.get("titles") or {}).items():
            titles[k] = " ".join(str(v or "").split())
        titles = {k: v for k, v in titles.items() if v}
        p = cycle.predict(log_, today)
        entries = cycle.calendar_plan(log_, today, tuple(titles), self.skipped(), self.max_day)
        if p is None or not entries:
            return [], None
        key = f"{p.last_start}:{p.expected}:{p.period_days}:" + ",".join(
            f"{k}={titles[k]}" for k in cycle.PLAN_KINDS if k in titles
        )
        return [(titles[e.kind], e.start, e.end) for e in entries], key

    def offer_plan(self, now: datetime) -> bool:
        """When the calendar plan changed since it was last offered, offer it in the group, in
        waking hours, with «Sí» and «No». Offered once per change: nobody
        answering does nothing, and it is not offered again until the prediction moves."""
        chat = self.chat_id
        if chat is None or not self.has_assistant or not self.profile.calendar.get("events"):
            return False
        local = now.astimezone(self.zone())
        if not PLAN_HOURS[0] <= local.hour < PLAN_HOURS[1]:
            return False
        today = cycle.bot_day(now, self.profile.timezone)
        entries, key = self.plan(self.repo.load(), today)
        if key is None or self.kv.get("calplan") == key:
            return False
        lines = "\n".join(
            "• " + self.entry_text(title, s.isoformat(), e.isoformat()) for title, s, e in entries
        )
        offer = {
            "key": secrets.token_hex(4),
            "kind": "plan",
            "plan": key,
            "entries": [
                {"title": title, "start": s.isoformat(), "end": e.isoformat()}
                for title, s, e in entries
            ],
            "for": [self.profile.user_id, *self.profile.admins],
            "items": [{"done": None}],
            "ts": self.now().timestamp(),
            "ttl": PLAN_TTL_S,
        }
        text = self.t("plan_offer", m=self.mention(), lines=lines)
        try:
            sent = self.send(chat, text, _keyboard(self.plan_rows(offer)))
        except TelegramError as e:
            log.warning("plan not offered: %s", safe(e))
            return False
        self.kv.set("calplan", key)
        offer["msg"] = (sent or {}).get("message_id")
        self.keep_offer(chat, offer)
        plain = re.sub(r"<[^>]+>", "", text)
        self.kv.add_chat(chat, "assistant", None, html.unescape(plain), int(now.timestamp()))
        log.info("calendar plan offered n=%d", len(entries))
        return True

    def tapped_plan(self, cq: Mapping, chat: int, data: str, user: object, role: str) -> None:
        """«Sí» under the plan: a turn in which the assistant adds the entries, each approved
        by this tap; «No»: nothing. A plan that moved since it was offered is
        not added: it is offered again."""
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        offer = next((o for o in self.offers(chat) if o.get("key") == key), None)
        if offer is None or self.now().timestamp() - offer.get("ts", 0) > offer.get(
            "ttl", PLAN_TTL_S
        ):
            self.answer(cq["id"], self.t("tap_gone"))
            if offer is None:
                self.unbutton(chat, mid)
            return
        if user not in offer.get("for", []):
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(offer["for"])))
            return
        if choice not in ("y", "n"):
            self.answer(cq["id"])
            return
        if choice == "y" and self.plan(self.repo.load(), self.today())[1] != offer.get("plan"):
            offer["items"][0]["done"] = "timeout"
            self.keep_offer(chat, offer)
            self.rekey(chat, offer.get("msg"), self.plan_rows(offer))
            self.kv.delete("calplan")  # the tick offers the new plan
            self.answer(cq["id"], self.t("plan_moved"))
            return
        offer["items"][0]["done"] = "calendar" if choice == "y" else "nothing"
        self.keep_offer(chat, offer)
        self.answer(cq["id"])
        self.rekey(chat, offer.get("msg"), self.plan_rows(offer))
        if choice == "n":
            log.info("calendar plan declined")
            return
        entries = offer.get("entries") or []
        listing = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
        grant = {"by": user, "events": [dict(e) for e in entries]}
        self.queue_tap(chat, mid, user, role, self.t("plan_calendar_turn", entries=listing), grant)
        log.info("calendar plan to the calendar")

    # --- edits ---------------------------------------------------------------------------

    def on_edit(self, msg: Mapping) -> None:
        """An edited text from her or an admin, handled by where its line is: still queued, the
        new wording goes; in the running turn, its open asks are declined and a correction joins
        it; already answered, a new line asks to check that answer; unknown, a new message when
        it now addresses the bot. An edit is never a command, and one that changes no word does
        nothing."""
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip() or text.strip().startswith("/"):
            return
        chat = msg.get("chat") or {}
        user = (msg.get("from") or {}).get("id")
        role = self.role(user)
        private = chat.get("type") == "private"
        if not private and chat.get("id") != self.chat_id:
            return
        if (role is None and private) or not self.has_assistant:
            return
        chat_id, mid = chat["id"], msg.get("message_id")
        body = self._strip_mention(msg, text)[0].strip()
        logged = self.kv.chat_find(chat_id, mid) if isinstance(mid, int) else None
        if role is None:  # someone else's group line: only the log takes the new wording
            if logged is not None:
                self.relog(logged, body, self.sender(msg))
            return
        key = f"pending:{chat_id}"
        found: list[dict] = []

        def swap(cur):
            out = []
            for x in cur or []:
                if x.get("id") == mid and "content" in x and "body" in x:
                    found.append(dict(x))
                    if _words(x["body"]) != _words(body):
                        head = x["content"][: len(x["content"]) - len(x["body"])]
                        if not x["content"].endswith(x["body"]):
                            head = f"{self.who(role, user)}: "
                        x = {**x, "content": head + body, "body": body}
                        x["ts"] = int(self.now().timestamp())
                out.append(x)
            return out or None

        with self._q:
            state = self._running.get(chat_id)
            running = state is not None and state.has(mid)
            if not running:
                self.kv.update_json(key, swap, [])
                if found:
                    self._newest[chat_id] = self.clock()
        name = self.who(role, user)
        if logged is not None and (found or logged["asked"]):
            self.relog(logged, body, name)
        if found:  # still queued: the debounce starts over with the new wording
            self.worker.submit(chat_id)
            log.info("edit: queued line")
            return
        line = {"mid": mid, "admin": role == ADMIN, "uid": user, "body": body}
        line["ts"] = int(self.now().timestamp())
        line["id"] = f"e{mid}.{secrets.token_hex(3)}"
        if running:
            old = state.edited.get(mid)
            if old is None:
                pending = self.kv.get_json(key, []) or []
                old = next((x.get("body", "") for x in pending if x.get("id") == mid), "")
            if _words(old) == _words(body):
                return
            state.edited[mid] = body
            line["content"] = (
                f"[{name} edited a message of this turn. Reconsider your answer with the new "
                f"wording.]\nBefore: {old}\nNow: {body}"
            )

            def correct(state=state, line=line, chat_id=chat_id, body=body):
                self.close_asks(state, f"editado: {body}")
                self.queue_line(chat_id, line)

            self.side.submit(correct)
            log.info("edit: running turn")
            return
        said = (self.kv.get_json(f"said:{chat_id}", {}) or {}).get(str(mid))
        if said is not None:
            if _words(said.get("body", "")) == _words(body):
                return
            line["content"] = (
                f"[{name} edited a message you already answered. Check whether your answer still "
                f"holds with the new wording, and say what changes.]\nBefore: {said.get('body', '')}"
                f"\nNow: {body}\nYour answer was: {said.get('answer', '')}"
            )
            self.queue_line(chat_id, line)
            log.info("edit: answered message")
            return
        content = self.addressed(msg, text, private, role)
        if content is not None:
            row = None
            if logged is not None:  # logged before (seen, or answered long ago): one row
                self.kv.chat_edit(logged["id"], content, body, asked=True)
                row = logged["id"]
            self.queue_text(chat_id, msg, content, role, body, row)
            log.info("edit: new message")
        elif logged is not None and not logged["asked"]:
            self.relog(logged, body, name)

    def relog(self, logged: Mapping, body: str, name: str) -> None:
        """An edited message's row in the chat log takes its new words, keeping what went in
        front of them (a reply's quote, the sender's name)."""
        old_text, old_body = str(logged.get("text") or ""), logged.get("body")
        if isinstance(old_body, str) and old_body and old_text.endswith(old_body):
            text = old_text[: len(old_text) - len(old_body)] + body
        else:
            text = f"{name}: {body}"
        self.kv.chat_edit(logged["id"], text, body)

    # --- voice notes (hearing thread) ----------------------------------------------------

    def resume(self) -> None:
        """At start: what a restart left in `pending:<chat>` is answered now, not when she
        writes again. Voice notes go to the hearing thread (which queues the turn after them);
        text lines, queued or taken by a turn that never finished, go to the worker."""
        for key in self.kv.keys("pending:"):
            chat = int(key.removeprefix("pending:"))
            lines = self.kv.get_json(key, []) or []
            if any("voice" in x for x in lines):
                self.hearer.submit(chat)
            elif self.has_assistant and any("content" in x for x in lines):
                self.worker.submit(chat)

    def _hear_chat(self, chat: int) -> None:
        """Hear every voice note waiting in the chat, then queue its turn: a note heard while a
        turn runs is handed to it, else it goes with the next one."""
        try:
            self.hear_all(chat, f"pending:{chat}")
        finally:
            self.worker.submit(chat)

    def hear_all(self, chat: int, key: str) -> None:
        """Turn every voice line waiting in `pending:<chat>` into a text line, oldest first. A
        note that cannot be heard leaves the list, with its reason posted under it."""
        for line in self.kv.get_json(key, []) or []:
            if "voice" not in line:
                continue
            try:
                content = self.hear(chat, line)
                row = None
                if content is not None:
                    row = self.kv.add_chat(
                        chat,
                        "user",
                        line.get("who"),
                        content,
                        int(line.get("ts") or self.now().timestamp()),
                        mid=line["id"] if isinstance(line["id"], int) else None,
                    )

                def settle(cur, line_id=line["id"], content=content, row=row):
                    out = []
                    for x in cur or []:
                        if x.get("id") == line_id and "voice" in x:
                            if content is None:
                                continue
                            x = {k: v for k, v in x.items() if k not in ("voice", "who")}
                            x["content"] = content
                            x["row"] = row
                        out.append(x)
                    return out or None

                self.kv.update_json(key, settle)
            finally:
                with self._q:
                    notes = self._voicing.get(chat) or {}
                    notes.pop(line["id"], None)
                    if not notes:
                        self._voicing.pop(chat, None)

    def hear(self, chat: int, line: Mapping) -> str | None:
        """One voice note: `getFile`, the download, the plugin's `transcribe`, then the «🎤»
        echo under the note. The line for the assistant, or None after posting why not:
        «voice_off» (no plugin, no `transcribe`, or `NotImplementedError`), «voice_too_long»
        (over 20 MiB) or «voice_failed» (anything else, an empty transcript too)."""
        p = self.profile
        block = p.assistant or {}
        voice = line["voice"]
        note = {"message_id": line["id"], "allow_sending_without_reply": True}
        reason = "voice_failed"
        text = ""
        try:
            info = self.tg.call("getFile", file_id=voice["file_id"])
            info = info if isinstance(info, Mapping) else {}
            size, path = info.get("file_size"), info.get("file_path")
            if isinstance(size, int) and size > VOICE_MAX:
                raise TelegramError("getFile", 413, "file too large")
            if not isinstance(path, str) or not path:
                raise TelegramError("getFile", 0, "no file path")
            audio = self.tg.download(path, VOICE_MAX)
            text = assistant.transcribe(
                self.data,
                block.get("plugin"),
                audio,
                voice["mime"],
                p.language,
                assistant.settings(block),
            ).strip()
        except NotImplementedError:
            reason = "voice_off"
        except TelegramError as e:
            log.warning("voice failed: %s", safe(e))
            too_big = e.code == 413 or "too big" in e.description.lower()
            reason = "voice_too_long" if too_big else "voice_failed"
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("voice failed: %s", safe(e))
        if not text:
            log.info("voice not heard: %s", reason)
            self.post(chat, self.t(reason), reply_parameters=note)
            return None
        log.info("voice heard")
        echo = split_text(text, ECHO_MAX)
        shown = echo[0] + ("…" if len(echo) > 1 else "")
        self.post(chat, self.t("voice_echo", text=html.escape(shown)), reply_parameters=note)
        return f"{line.get('who') or ''}: [voice note] {text}"

    def say(
        self,
        chat: int,
        text: str,
        reply_to: int | None,
        offered: Sequence[str] = (),
        users: Sequence[int] = (),
    ) -> bool:
        """An assistant answer: plain text, a reply to her message, split at Telegram's limit.
        With choices (`offered`) its last part carries them as buttons, for `users` to tap, and a
        line saying they may answer in their own words instead; without, one that ends with a
        question carries a selective force_reply, so her answer reaches the bot too. False when a
        part could not be sent."""
        key = secrets.token_hex(4) if offered else None
        if offered:
            hint = self.t("yes_no_hint" if self.yes_no(offered) else "options_hint")
            text = f"{text}\n\n{hint}"
        parts = split_text(text)
        for i, part in enumerate(parts):
            markup = None
            if i == len(parts) - 1 and key:
                markup = _keyboard([[(label, f"o:{key}:{n}")] for n, label in enumerate(offered)])
            elif i == len(parts) - 1 and text.rstrip().endswith("?"):
                markup = {"force_reply": True, "selective": True}
            parameters = None
            if reply_to and i == 0:
                parameters = {"message_id": reply_to, "allow_sending_without_reply": True}
            try:
                sent = self.tg.call(
                    "sendMessage",
                    chat_id=chat,
                    text=part,
                    reply_parameters=parameters,
                    reply_markup=markup,
                )
            except TelegramError as e:
                log.warning("send failed: %s", safe(e))
                return False
        if key:
            msg = sent.get("message_id") if isinstance(sent, Mapping) else None
            self.offer(chat, msg, key, offered, users)
        return True

    def apply_block(self, chat: int, block: object, source: str, today: date) -> None:
        """Validate and write a record block, all or nothing, then «Anotado:» from the
        `Change` (none when nothing changed), the closing line and the pain red flag."""
        try:
            if block is assistant.BAD_BLOCK:
                raise BlockError("bad_json")
            edit = records.validate_block(block, today)
            change = self.write(lambda log_: records.apply(log_, edit, source))
        except BlockError as e:
            log.warning("block refused: %s", e.code)
            self.post(chat, self.t("invalid_block"), self.today_keyboard())
            return
        except (ValueError, OSError) as e:  # an unreadable log, or the disk refused the write
            log.warning("block failed: %s", safe(e))
            self.post(chat, self.t("invalid_block"), self.today_keyboard())
            return
        text = self.recorded_text(change)
        if text:
            self.post(chat, text)
        self.announce(change, chat)

    # --- settings ------------------------------------------------------------------------

    def apply_settings(self, chat: int, block: object) -> bool:
        """A `settings` block or /hora: check it all or nothing, rewrite `profile.yaml` by line,
        commit it (`settings: ask_at 21:30`), push, and use the new profile at once (the
        scheduler reads `ask_at` on its next tick). «invalid_settings» when the block is
        refused, «toast_error» when the file could not be written or committed."""
        try:
            if block is assistant.BAD_BLOCK:
                raise ValueError("bad_json")
            values = validate_settings(block)
        except ValueError as e:
            log.warning("settings refused: %s", e)
            self.post(chat, self.t("invalid_settings"))
            return False
        profile: list[Profile] = []

        def rewrite(old: str) -> str:
            new = set_profile_lines(old, values)
            p = Profile.from_dict(yaml.safe_load(new))
            if profile_errors(p, self.data) or any(getattr(p, k) != v for k, v in values.items()):
                raise ValueError("profile_not_editable")
            profile.append(p)
            return new

        message = ", ".join(f"{k} {v}" for k, v in values.items())
        try:
            self.repo.write_profile(rewrite, message)
        except (ValueError, yaml.YAMLError, GitError, OSError) as e:
            log.warning("settings failed: %s", safe(e))
            self.post(chat, self.t("toast_error"))
            return False
        self.profile = profile[0]
        log.info("settings applied keys=%d", len(values))
        try:
            self.repo.flush()
        except Exception as e:  # noqa: BLE001 - the next tick pushes it
            log.warning("git sync failed: %s", safe(e))
        what = "; ".join(self.t(f"settings_{k}", time=v, n=v) for k, v in values.items())
        self.post(chat, self.t("settings_done", what=what))
        return True

    def reload_profile(self) -> bool:
        """After a pull changed `profile.yaml`: use the new one, unless it fails the same
        checks `sct check` makes; then the old one stays and the log says so."""
        try:
            p = load_profile(self.data)
            errors = profile_errors(p, self.data)
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            errors = ["bad_profile"]
        if errors:
            log.warning("profile reload refused: %s", ", ".join(errors))
            return False
        if p.language != self.profile.language:
            self.t = Translator(p.language)
        self.profile = p
        log.info("profile reloaded")
        return True


def split_text(text: str, limit: int = TEXT_MAX) -> list[str]:
    """`text` in parts of at most `limit` UTF-16 code units, cut between paragraphs where it
    can and inside one only when a paragraph alone is too long."""
    parts, current = [], ""
    for para in text.split("\n\n"):
        joined = f"{current}\n\n{para}" if current else para
        if _units(joined) <= limit:
            current = joined
            continue
        if current:
            parts.append(current)
        while _units(para) > limit:
            cut = limit
            while _units(para[:cut]) > limit:
                cut -= 1
            parts.append(para[:cut])
            para = para[cut:]
        current = para
    if current:
        parts.append(current)
    return parts


def _top(counts: Counter, defaults: Sequence[str]) -> list[str]:
    """The six most frequent symptoms in `counts`, padded with `defaults` in their order."""
    top = sorted(counts, key=lambda s: (-counts[s], SYMPTOMS.index(s)))[:SYMPTOM_BUTTONS]
    for s in defaults:
        if len(top) >= SYMPTOM_BUTTONS:
            break
        if s not in top:
            top.append(s)
    return top


def eve_counts(log_: Log, back: Sequence[int]) -> Counter:
    """How often each symptom was logged `back` days before a start (1 = the day before)."""
    counts: Counter = Counter()
    for start in records.starts(log_):
        for n in back:
            day = log_.get(start - timedelta(days=n))
            if day is not None:
                counts.update(day.symptoms)
    return counts


def eve_starts(log_: Log) -> int:
    """How many starts have a symptom logged on the day before."""
    eves = (log_.get(s - timedelta(days=1)) for s in records.starts(log_))
    return sum(1 for day in eves if day is not None and day.symptoms)


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _ago(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


# --- /anotar -------------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lower case without accents, so «miercoles» finds «miércoles»."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _phrase(tokens: list[str], i: int, phrases: Mapping[str, object]) -> tuple[object, int]:
    """The longest phrase of `phrases` (folded text -> value) at `tokens[i:]`: (value, length),
    or (None, 0)."""
    for j in range(len(tokens), i, -1):
        key = " ".join(tokens[i:j])
        if key in phrases:
            return phrases[key], j - i
    return None, 0


def _when(tokens: list[str], t: Translator, today: date) -> tuple[date, int]:
    """`cuándo` at the start of `tokens`: hoy/ayer/anteayer, a weekday (the most recent past
    one), D/M (the most recent such date) or YYYY-MM-DD."""
    back = {"today": 0, "yesterday": 1, "day_before": 2}
    phrases = {fold(w): back[k] for k, words in t("words_when").items() for w in words}
    offset, n = _phrase(tokens, 0, phrases)
    if n:
        return today - timedelta(days=offset), n
    first = tokens[0]
    for names in (t("days"), t("days_short")):
        folded = [fold(x) for x in names]
        if first in folded:
            days = (today.weekday() - folded.index(first)) % 7 or 7
            return today - timedelta(days=days), 1
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", first)
    if m:
        d = date(today.year, int(m.group(2)), int(m.group(1)))
        return (d if d <= today else d.replace(year=today.year - 1)), 1
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", first):
        return date.fromisoformat(first), 1
    raise ValueError("bad_when")


def parse_anotar(text: str, t: Translator, today: date) -> tuple[date, dict]:
    """`<cuándo> <cuánto> [dolor] [síntomas…]` from the locale's own words: a flow word, an
    integer 0..10 for the pain («dolor 6» too), symptom labels matched by prefix. Raises
    `ValueError` with a reason code on anything it does not understand."""
    tokens = [fold(x) for x in text.split()]
    if not tokens:
        raise ValueError("empty")
    try:
        d, i = _when(tokens, t, today)
    except (ValueError, OverflowError):
        raise ValueError("bad_when") from None
    if not records.in_range(d, today):
        raise ValueError("date_out_of_range")
    flows = {fold(w): k for k, words in t("words_flow").items() for w in words}
    labels = {fold(t.symptom(s)): s for s in SYMPTOMS}
    pain_word = fold(t("pain", n="")).strip()
    fields: dict = {}
    symptoms: list[str] = []
    while i < len(tokens):
        token = tokens[i]
        if token == pain_word and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            i += 1
            token = tokens[i]
        if token.isdigit():
            if int(token) > 10 or "pain" in fields:
                raise ValueError("bad_pain")
            fields["pain"] = int(token)
            i += 1
            continue
        flow, n = _phrase(tokens, i, flows)
        if n:
            if "flow" in fields:
                raise ValueError("two_flows")
            fields["flow"] = flow
            i += n
            continue
        symptom, n = _symptom(tokens, i, labels)
        symptoms.append(symptom)
        i += n
    if symptoms:
        fields["symptoms"] = tuple(sorted(set(symptoms)))
    if not fields:
        raise ValueError("nothing_to_note")
    return d, fields


def _symptom(tokens: list[str], i: int, labels: Mapping[str, str]) -> tuple[str, int]:
    """The symptom whose label the longest run of words at `tokens[i:]` names: the label
    itself, or the start of exactly one label."""
    for j in range(len(tokens), i, -1):
        words = " ".join(tokens[i:j])
        if words in labels:
            return labels[words], j - i
        found = {s for label, s in labels.items() if label.startswith(words)}
        if len(found) == 1:
            return found.pop(), j - i
    raise ValueError("unknown_word")


def _same_facts(a: Day, b: Day) -> bool:
    return (a.flow, a.pain, a.symptoms, a.note) == (b.flow, b.pain, b.symptoms, b.note)
