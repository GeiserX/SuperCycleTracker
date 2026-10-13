"""The bot: the cards with their buttons, free text for the assistant, and the worker thread.

A tap is recorded at once, with no AI: each button carries its date and value, and the next step is
computed from the log, never from a session table, so a stale keyboard still works and a restart
mid-card loses nothing. Text meant for the bot is queued in SQLite (`pending:<chat>`) and one worker
thread runs assistant turns, one at a time, so a tap is answered while the assistant thinks.

The service log gets event kinds and counts only: never a message text, a date of a record or a
value.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from supercycletracker import assistant, cycle, records
from supercycletracker.i18n import Translator
from supercycletracker.records import SYMPTOMS, BlockError, Change, Day, Edit, Log
from supercycletracker.repo import DATA_LOCK, KV, STATE_DB, Repo
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
PAIN_HIGH = 8  # a day crossing into this pain or more posts `pain_high`
TEXT_MAX = 4096  # Telegram's limit on one message, in UTF-16 code units
NOT_MODIFIED = "message is not modified"
HER, ADMIN = "her", "admin"

_COMMAND = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$", re.DOTALL)
_CALLBACK_DATE = re.compile(r"^\d{8}$")
_VALUES = {
    "y": frozenset({"unspecified", "light", "medium", "heavy"}),
    "f": frozenset({"none", "light", "medium", "heavy", "same"}),
    "p": frozenset(str(n) for n in range(11)),
    "s": frozenset(SYMPTOMS),
}
_NO_VALUE = frozenset({"d", "pick", "back", "wait", "skip", "fix", "more"})


# --- the profile ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """The keys of `cycle/profile.yaml` the app reads; unknown keys are ignored."""

    name: str
    timezone: str = "UTC"
    language: str = "en"
    ask_at: str = "21:00"
    birth_date: date | None = None
    chat_id: int | None = None
    user_id: int | None = None
    username: str | None = None
    admins: Mapping[int, str] = field(default_factory=dict)
    assistant: Mapping | None = None

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
        username = tg.get("username")
        block = raw.get("assistant")
        return cls(
            name=str(raw.get("name") or ""),
            timezone=str(raw.get("timezone") or "UTC"),
            language=str(raw.get("language") or "en"),
            ask_at=_clock_text(raw.get("ask_at")),
            birth_date=birth if isinstance(birth, date) else None,
            chat_id=_int(tg.get("chat_id")),
            user_id=_int(tg.get("user_id")),
            username=str(username).lstrip("@") if username else None,
            admins={int(k): str(v) for k, v in (tg.get("admins") or {}).items()},
            assistant=dict(block) if isinstance(block, Mapping) else None,
        )


def load_profile(data: str | Path) -> Profile:
    path = Path(data) / PROFILE_FILE
    return Profile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _int(value: object) -> int | None:
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

    def __init__(self, fn: Callable[[int], None]):
        self._fn = fn
        self._cond = threading.Condition()
        self._queue: deque[int] = deque()
        self._busy = False
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name="sct-worker", daemon=True)
        self._thread.start()

    def submit(self, chat: int) -> None:
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


class Bot:
    def __init__(
        self,
        data: str | Path,
        tg,
        *,
        now: Callable[[], datetime] | None = None,
        token: str = "",
        profile: Profile | None = None,
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
        self.commands: dict[str, Callable] = {"hoy": self.cmd_hoy, "today": self.cmd_hoy}
        self.worker = Worker(self._turn)

    def start(self) -> None:
        me = self.tg.call("getMe") or {}
        self.bot_id = me.get("id")
        self.username = str(me.get("username") or "")
        self.read_all = bool(me.get("can_read_all_group_messages"))
        log.info("bot @%s up", self.username)
        log.info("group privacy %s", "off: every group message arrives" if self.read_all else "on")

    def close(self) -> None:
        self.worker.stop()
        self.kv.close()

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

    def phase(self, log_: Log, today: date) -> cycle.Phase:
        return cycle.phase(log_, today, self.skipped())

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
        counts = Counter(s for day in log_.values() for s in day.symptoms)
        top = sorted(counts, key=lambda s: (-counts[s], SYMPTOMS.index(s)))[:SYMPTOM_BUTTONS]
        for s in DEFAULT_SYMPTOMS:
            if len(top) >= SYMPTOM_BUTTONS:
                break
            if s not in top:
                top.append(s)
        return top

    def kb_symptoms(self, d: date, log_: Log) -> dict:
        t, k = self.t, _key(d)
        chosen = set(log_[d].symptoms) if d in log_ else set()
        shown = self.top_symptoms(log_)
        shown += [s for s in SYMPTOMS if s in chosen and s not in shown]  # one chosen in words
        buttons = [(("✓ " if s in chosen else "") + t.symptom(s), f"s:{k}:{s}") for s in shown]
        rows = _rows(buttons, 3)
        rows.append([(t("btn_other"), f"more:{k}"), (t("btn_done"), f"d:{k}")])
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
        if ph.kind == "period" and ph.day == cycle.CATCHUP_DAY:
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
            dates = cycle.queue(ph, log_, today)
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
        if ph.kind == "period" and ph.day == cycle.PERIOD_MAX_DAY:
            text += " " + t("day7")
        elif ph.kind == "period" and ph.day is not None and ph.day < cycle.PERIOD_MAX_DAY:
            text += " " + t("tomorrow", time=self.profile.ask_at)
        return text

    # --- updates -----------------------------------------------------------------------

    def handle(self, update: Mapping) -> None:
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

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

    def _tap_s(self, tap: Tap, d: date, value: str, today: date) -> None:
        def toggle(log_: Log) -> Change:
            current = set(log_[d].symptoms) if d in log_ else set()
            current ^= {value}
            return records.apply(log_, Edit(days={d: {"symptoms": tuple(current)}}), tap.source)

        change = self.write(toggle)
        self.answer(tap.id, self.t("toast_saved", what=self.t.symptom(value)))
        self.symptoms_step(tap, d, change.after)
        self.announce(change, tap.chat)

    def _tap_d(self, tap: Tap, d: date, value: None, today: date) -> None:
        self.answer(tap.id)
        self.advance(tap, d, self.repo.load(), today)

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

    def next_date(self, d: date, log_: Log, today: date) -> date | None:
        ph = self.phase(log_, today)
        dates = [x for x in cycle.queue(ph, log_, today) if x != d]
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
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        chat = msg.get("chat") or {}
        user = (msg.get("from") or {}).get("id")
        role = self.role(user)
        if role is None:
            return  # anyone else gets no answer
        private = chat.get("type") == "private"
        if not private and chat.get("id") != self.chat_id:
            return
        command = _COMMAND.match(text.strip())
        if command:
            name, handle, rest = command.group(1).lower(), command.group(2), command.group(3)
            if handle and handle.lower() != self.username.lower():
                return  # a command for another bot
            handler = self.commands.get(name)
            if handler is not None:
                handler(msg, (rest or "").strip(), role)
            return
        content = self.addressed(msg, text, private, role)
        if content is not None:
            self.queue_text(chat["id"], msg, content, role)

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

    def _strip_mention(self, msg: Mapping, text: str) -> tuple[str, bool]:
        """Remove each `mention` entity equal to the bot's @username. Entity offsets count
        UTF-16 code units, so the text is cut in UTF-16."""
        if not self.username:
            return text, False
        raw = text.encode("utf-16-le")
        target = "@" + self.username.lower()
        spans = []
        for e in msg.get("entities") or []:
            if e.get("type") != "mention":
                continue
            a, b = 2 * int(e.get("offset", 0)), 2 * int(e.get("offset", 0) + e.get("length", 0))
            if raw[a:b].decode("utf-16-le", "replace").lower() == target:
                spans.append((a, b))
        for a, b in sorted(spans, reverse=True):
            raw = raw[:a] + raw[b:]
        return raw.decode("utf-16-le", "replace"), bool(spans)

    def queue_text(self, chat: int, msg: Mapping, content: str, role: str) -> None:
        """Append a line to `pending:<chat>` (the newest 10, none older than 7 days) and queue
        the chat for the worker. Without an assistant: «no_ai» with today's keyboard."""
        if not self.has_assistant:
            self.post(chat, self.t("no_ai"), self.today_keyboard())
            log.info("text without assistant")
            return
        now = int(self.now().timestamp())
        line = {"id": msg.get("message_id"), "ts": now, "content": content, "admin": role == ADMIN}
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
        log.info("text queued")
        self.worker.submit(chat)

    def cmd_hoy(self, msg: Mapping, rest: str, role: str) -> None:
        """/hoy: today's card; with text after it, the text goes to the assistant."""
        chat = msg["chat"]["id"]
        if rest:
            user = (msg.get("from") or {}).get("id")
            self.queue_text(chat, msg, f"{self.who(role, user)}: {rest}", role)
        else:
            self.send_card(chat, self.today())

    # --- the assistant turn (worker thread) ----------------------------------------------

    def _turn(self, chat: int) -> None:
        """One turn: every pending line of the chat goes as `new`. On success the text is
        posted, the block applied and the lines taken removed (lines that arrived meanwhile
        stay). On an exception, a timeout or `kept: false`: «ai_down» with the keyboard, no
        block, and the lines stay for the next turn."""
        key = f"pending:{chat}"
        lines = self.kv.get_json(key, []) or []
        if not lines:
            return
        p = self.profile
        block_cfg = p.assistant or {}
        try:
            self.tg.call("sendChatAction", chat_id=chat, action="typing")
        except TelegramError as e:
            log.warning("sendChatAction failed: %s", safe(e))
        started = time.monotonic()
        now = self.now()
        today = cycle.bot_day(now, p.timezone)
        try:
            with DATA_LOCK:
                try:
                    log_text = self.repo.log_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    log_text = ""
            log_ = records.loads(log_text)
            admins = list(p.admins.values())
            context = assistant.context(
                log_,
                now,
                tz=p.timezone,
                t=self.t,
                name=p.name,
                admins=admins,
                birth_date=p.birth_date,
                ask_at=p.ask_at,
                asked=self.kv.get("last_ask") == today.isoformat(),
                skipped=self.skipped(),
            )
            rules = assistant.rules(p.name, p.language, admins, block_cfg.get("instructions") or "")
            turn = assistant.turn(
                self.kv.get(f"conv:{chat}"),
                rules,
                context,
                log_text,
                [x["content"] for x in lines],
            )
            reply = assistant.converse(
                self.data, block_cfg.get("plugin"), turn, assistant.settings(block_cfg)
            )
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("ai turn failed: %s", safe(e))
            self.post(chat, self.t("ai_down"), self.today_keyboard())
            return
        if not reply.kept:
            log.warning("ai turn not kept")
            self.post(chat, self.t("ai_down"), self.today_keyboard())
            return
        log.info("ai turn ok %ds", round(time.monotonic() - started))
        self.last_ai = self.now()
        if reply.conversation:
            self.kv.set(f"conv:{chat}", reply.conversation)
        else:
            self.kv.delete(f"conv:{chat}")
        text, block = assistant.extract(reply.text)
        if text and not self.say(chat, text, lines[-1]["id"]):
            # she never saw the answer: a failed turn, so the buttons and the lines stay
            self.post(chat, self.t("ai_down"), self.today_keyboard())
            return
        if block is not None:
            source = "admin" if all(x.get("admin") for x in lines) else "telegram"
            self.apply_block(chat, block, source, today)
        # last, so a crash before here sends the lines again; a block applied twice changes
        # nothing the second time
        taken = {x["id"] for x in lines}
        self.kv.update_json(key, lambda cur: [x for x in cur or [] if x["id"] not in taken] or None)

    def say(self, chat: int, text: str, reply_to: int | None) -> bool:
        """An assistant answer: plain text, a reply to her message, split at Telegram's limit.
        One that ends with a question carries a selective force_reply on its last part, so her
        answer reaches the bot too. False when a part could not be sent."""
        parts = split_text(text)
        for i, part in enumerate(parts):
            markup = None
            if i == len(parts) - 1 and text.rstrip().endswith("?"):
                markup = {"force_reply": True, "selective": True}
            parameters = None
            if reply_to and i == 0:
                parameters = {"message_id": reply_to, "allow_sending_without_reply": True}
            try:
                self.tg.call(
                    "sendMessage",
                    chat_id=chat,
                    text=part,
                    reply_parameters=parameters,
                    reply_markup=markup,
                )
            except TelegramError as e:
                log.warning("send failed: %s", safe(e))
                return False
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


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _same_facts(a: Day, b: Day) -> bool:
    return (a.flow, a.pain, a.symptoms, a.note) == (b.flow, b.pain, b.symptoms, b.note)
