"""The assistant plugin: loading it, the turn it gets, and the record block it may send back.

The plugin is one Python file in the data directory (`profile.yaml` `assistant.plugin`). The app
never knows which provider sits behind it: it hands over the rules, a fresh `context.md`, the whole
log and her new lines, and takes back text that may end with one fenced `record` block. The app,
not the assistant, validates and applies that block.

Nothing here logs: errors are `PluginError` reason codes, and a plugin's own exception goes up to
the caller, which logs its class only (`telegram.safe`).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import statistics
import sys
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from supercycletracker import cycle
from supercycletracker.i18n import Translator
from supercycletracker.records import SYMPTOMS, Day

RULES = Path(__file__).parent / "rules.md"
LOG_KEY = "cycle/log.jsonl"
DEFAULT_TIMEOUT_S = 600
CALENDAR_DAYS = 14
NOT_SETTINGS = ("plugin", "instructions")  # the profile keys a plugin's settings leave out
LANGUAGE_NAMES = {"en": "English", "es": "Spanish"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_BLOCK = re.compile(r"```record[^\S\n]*\n(.*?)```", re.DOTALL)
_UNCLOSED = re.compile(r"```record\b.*\Z", re.DOTALL)
BAD_BLOCK = object()  # `extract` found a block whose JSON does not parse


class PluginError(Exception):
    """The plugin could not be loaded or answered wrongly. The message is a reason code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- loading -------------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _module(path: str, mtime_ns: int) -> ModuleType:
    """One module per plugin file version: state the plugin keeps at module level survives
    between calls, and an edited file (a data repository pull) is loaded again.

    The module sits in `sys.modules` under a name unique to this version before it runs, as a
    normal import does: `dataclasses` and `typing` look the module up there by name."""
    digest = hashlib.sha256(path.encode()).hexdigest()[:12]
    name = f"sct_plugin_{digest}_{mtime_ns}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginError("plugin_load_failed")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def load_module(data: str | Path, rel: str | None) -> ModuleType:
    """The plugin module at `rel`, which must resolve inside the data directory."""
    if not rel or not isinstance(rel, str):
        raise PluginError("no_plugin")
    root = Path(data).resolve()
    path = (root / rel).resolve()
    if root not in path.parents:
        raise PluginError("plugin_outside_data")
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        raise PluginError("plugin_missing") from None
    try:
        return _module(str(path), mtime_ns)
    except PluginError:
        raise
    except Exception:  # noqa: BLE001 - a plugin's import error may quote anything
        raise PluginError("plugin_load_failed") from None


def function(data: str | Path, rel: str | None, name: str, required: bool = True):
    fn = getattr(load_module(data, rel), name, None)
    if not callable(fn):
        if required:
            raise PluginError(f"plugin_has_no_{name}")
        return None
    return fn


def settings(assistant: Mapping | None) -> dict:
    """The `assistant:` block of the profile minus `plugin` and `instructions`."""
    return {k: v for k, v in (assistant or {}).items() if k not in NOT_SETTINGS}


def timeout_s(plugin_settings: Mapping) -> float:
    value = plugin_settings.get("timeout_s")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return float(DEFAULT_TIMEOUT_S)


# --- calling -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    text: str
    conversation: str | None = None
    kept: bool = True


def reply(out: object) -> Reply:
    """A plugin's return value: a bare string, or `{"text", "conversation", "kept"}`."""
    if isinstance(out, str):
        return Reply(out)
    if isinstance(out, Mapping):
        text, conversation, kept = out.get("text"), out.get("conversation"), out.get("kept", True)
        if not isinstance(text, str) or not isinstance(kept, bool):
            raise PluginError("bad_reply")
        if conversation is not None and not isinstance(conversation, str):
            raise PluginError("bad_reply")
        return Reply(text, conversation or None, kept)
    raise PluginError("bad_reply")


_ABANDONED: dict[str, threading.Thread] = {}  # per plugin path: a call that outlived its wait


def converse(data: str | Path, rel: str | None, turn: dict, plugin_settings: dict) -> Reply:
    """Run the plugin's `converse` and wait at most `timeout_s`.

    The call runs in its own daemon thread because the app cannot stop a plugin: one that
    outlives the wait is abandoned and the turn counts as failed (`PluginError("timeout")`).
    Until that call returns, the plugin is not called again (`PluginError("plugin_busy")`), so
    it never runs two turns at once. The plugin's own exception is raised as it is; an exit that
    is not an `Exception` (`SystemExit`, `KeyboardInterrupt`) becomes `PluginError("plugin_aborted")`."""
    fn = function(data, rel, "converse")
    key = str((Path(data) / str(rel)).resolve())
    previous = _ABANDONED.get(key)
    if previous is not None and previous.is_alive():
        raise PluginError("plugin_busy")
    box: dict = {}

    def run():
        try:
            box["out"] = fn(turn, plugin_settings)
        except BaseException as e:  # noqa: BLE001 - handed to the caller as it is
            box["error"] = e

    thread = threading.Thread(target=run, name="sct-plugin", daemon=True)
    thread.start()
    thread.join(timeout_s(plugin_settings))
    if thread.is_alive():
        _ABANDONED[key] = thread
        raise PluginError("timeout")
    _ABANDONED.pop(key, None)
    error = box.get("error")
    if error is not None:
        if not isinstance(error, Exception):  # SystemExit and the like must not end the worker
            raise PluginError("plugin_aborted")
        raise error
    return reply(box.get("out"))


def forget(data: str | Path, rel: str | None, conversation: str, plugin_settings: dict) -> None:
    """Tell the plugin to drop a conversation, when it defines `forget`."""
    fn = function(data, rel, "forget", required=False)
    if fn is not None:
        fn(conversation, plugin_settings)


# --- the turn ------------------------------------------------------------------------------


def turn(
    conversation: str | None, rules_text: str, context_text: str, log_text: str, new: Sequence[str]
) -> dict:
    """The dict a plugin's `converse` gets (the frozen contract): her lines only, oldest first."""
    return {
        "conversation": conversation,
        "rules": rules_text,
        "context": context_text,
        "files": {LOG_KEY: log_text},
        "new": [{"role": "user", "content": content} for content in new],
    }


def rules(name: str, language: str, others: Sequence[str] = (), instructions: str = "") -> str:
    """`rules.md` filled in. Plain replacement, not `str.format`: the text holds JSON braces."""
    values = {
        "name": name,
        "language_name": LANGUAGE_NAMES.get(language, language),
        "others": _join(others) or "her family",
        "symptom_ids": ", ".join(SYMPTOMS),
        "instructions": (instructions or "").strip(),
    }
    text = RULES.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text.rstrip() + "\n"


def extract(text: str) -> tuple[str, object]:
    """Take the record blocks out of an answer: `(clean text, the last block)`.

    The block is the parsed JSON, `BAD_BLOCK` when it does not parse (or the fence never
    closes), or None without a block. Every block leaves the text, so she never sees one."""
    blocks = _BLOCK.findall(text)
    clean = _BLOCK.sub("", text)
    unclosed = _UNCLOSED.search(clean)
    block: object = None
    if unclosed:
        clean = clean[: unclosed.start()]
        block = BAD_BLOCK
    elif blocks:
        try:
            block = json.loads(blocks[-1])
        except ValueError:
            block = BAD_BLOCK
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, block


# --- context.md ----------------------------------------------------------------------------


def context(
    log: Mapping[date, Day],
    now: datetime,
    *,
    tz: str,
    t: Translator,
    name: str,
    admins: Sequence[str] = (),
    birth_date: date | None = None,
    ask_at: str = "21:00",
    asked: bool = False,
    skipped: Collection[date] = (),
) -> str:
    """`context.md`, rebuilt every turn: the date, where she is, the prediction, a calendar of
    the last two weeks, her symptoms and the people. Ids and flows stay English; weekdays come
    in both languages so the assistant can resolve «el martes»."""
    local = now.astimezone(ZoneInfo(tz))
    today = cycle.bot_day(now, tz)
    ph = cycle.phase(log, today, skipped)
    local_names = t("days")
    first_days = [d for d, day in sorted(log.items()) if day.cycle_start and d <= today]

    wd = local.date().weekday()
    head = (
        f"{WEEKDAYS[wd]} ({local_names[wd]}) {local.date().isoformat()}, {local:%H:%M} {tz}. "
        f"Bot day {today.isoformat()} (a day ends at 05:00)."
    )
    if birth_date is not None:
        head += f" She is {_age(birth_date, today)}."
    out = ["# Today", head, "", "# Where she is", _where(ph, today)]
    answered = today in log and log[today].flow is not None
    if asked:
        state = "answered." if answered else "not answered yet."
        out.append(f"The bot asked at {ask_at} about today; {state}")
    else:
        out.append(f"The bot has not asked today (it asks at {ask_at} when a period is due).")
    if ph.missing:
        out.append("Missing days the bot still wants: " + ", ".join(_day(d) for d in ph.missing))

    p = ph.prediction
    out.append("")
    if p is None:
        out += ["# Prediction", "None yet: the log has no cycle start."]
    else:
        prefix = "After this period: next start" if ph.kind == "period" else "Next start"
        if len(p.lengths) >= cycle.MIN_LENGTHS:
            lengths = ", ".join(str(x) for x in p.lengths)
            span = f"range {min(p.lengths)}-{max(p.lengths)}"
            out.append(f"# Prediction (last {len(p.lengths)} cycles)")
            out.append(
                f"Cycle lengths: {lengths} (median {p.median}, {span}). "
                f"Usual period: {p.period_days} days."
            )
        else:
            out.append("# Prediction")
            out.append(
                f"Fewer than {cycle.MIN_LENGTHS} usable cycle lengths, so it assumes "
                f"{p.median} days. Usual period: {p.period_days} days."
            )
        out.append(
            f"{prefix} around {p.expected.isoformat()} "
            f"(window {p.window_start.isoformat()} .. {p.window_end.isoformat()})."
        )

    out += ["", '# Calendar, last 14 days (use it for "ayer", "el martes"...)']
    short = t("days_short")
    for i in range(CALENDAR_DAYS):
        d = today - timedelta(days=i)
        tag = " (today)" if i == 0 else ""
        desc = _describe(log.get(d), _cycle_day(d, first_days))
        out.append(f"{short[d.weekday()]} {d.isoformat()}{tag}: {desc}")

    out += ["", "# Her symptoms over the log", _symptoms(log, first_days)]
    people = [f"{name} (whose cycle this is)", *(f"{a} (admin)" for a in admins)]
    out += ["", "# People", "; ".join(people)]
    return "\n".join(out) + "\n"


def _day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.isoformat()}"


def _where(ph: cycle.Phase, today: date) -> str:
    p = ph.prediction
    if ph.kind == "period":
        return f"Period day {ph.day} (started {_day(ph.start)})."
    if ph.kind == "no_history" or p is None:
        return "No cycle start in the log yet."
    if ph.kind == "waiting":
        return (
            f"No period open; the window for the next one is open "
            f"({p.window_start.isoformat()} .. {p.window_end.isoformat()})."
        )
    if ph.kind == "late":
        return f"No period open; it is late (the window closed on {p.window_end.isoformat()})."
    return f"No period open; the last one started {_day(ph.start)}."


def _cycle_day(d: date, first_days: Sequence[date]) -> int | None:
    before = [s for s in first_days if s <= d]
    return (d - before[-1]).days + 1 if before else None


def _describe(day: Day | None, n: int | None) -> str:
    if day is None:
        return "-"
    parts = []
    if day.flow is not None:
        parts.append(day.flow)
    if day.pain is not None:
        parts.append(f"pain {day.pain}")
    parts += day.symptoms
    if day.note:
        parts.append(f"note: {json.dumps(day.note, ensure_ascii=False)}")
    text = ", ".join(parts) or "-"
    if day.bleed and n is not None:
        text += f" [day {n}]"
    return text


def _symptoms(log: Mapping[date, Day], first_days: Sequence[date]) -> str:
    """Each symptom with the number of cycles it was logged in and its usual cycle days."""
    cycles: dict[str, set[date | None]] = {}
    days: dict[str, list[int]] = {}
    for d, day in sorted(log.items()):
        before = [s for s in first_days if s <= d]
        for s in day.symptoms:
            cycles.setdefault(s, set()).add(before[-1] if before else None)
            if before:
                days.setdefault(s, []).append((d - before[-1]).days + 1)
    if not cycles:
        return "None logged yet."
    order = sorted(cycles, key=lambda s: (-len(cycles[s]), SYMPTOMS.index(s)))
    items = []
    for s in order:
        n = len(cycles[s])
        text = f"{s} in {n} cycle{'s' if n != 1 else ''}"
        if days.get(s):
            values = sorted(days[s])
            lo = values[len(values) // 4]
            hi = values[(3 * len(values)) // 4]
            mid = round(statistics.median(values))
            text += f" (usually day {lo}-{hi})" if lo != hi else f" (usually day {mid})"
        items.append(text)
    return "; ".join(items) + "."


def _age(birth: date, today: date) -> int:
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _join(items: Sequence[str]) -> str:
    items = [x for x in items if x]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
