"""The `sct` command: run the bot, check a data directory, print the prediction, import an Apple
Health export, and send a test card.

Output names counts and reason codes. Only `predict`, which a person runs to see the prediction,
prints dates.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from supercycletracker import __version__, applehealth, cycle, records
from supercycletracker.repo import LOG_FILE

ALIVE = Path("/tmp/sct.alive")  # the Dockerfile's HEALTHCHECK reads its age


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sct", description="SuperCycleTracker")
    parser.add_argument("--version", action="version", version=f"sct {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    data = argparse.ArgumentParser(add_help=False)
    data.add_argument("--data", default=os.environ.get("SCT_DATA", "/data"), type=Path)
    sub.add_parser("run", parents=[data], help="run the bot (TELEGRAM_BOT_TOKEN, SCT_GIT_TOKEN)")
    sub.add_parser("check", parents=[data], help="validate profile.yaml and the log")
    pr = sub.add_parser("predict", parents=[data], help="print the prediction")
    pr.add_argument("--today", type=date.fromisoformat, help="YYYY-MM-DD, default the bot day")
    im = sub.add_parser("import-apple-health", parents=[data], help="merge an Apple Health export")
    im.add_argument("export", type=Path, help="export.zip or export.xml")
    im.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    say = sub.add_parser("say", parents=[data], help="send a message as the bot")
    say.add_argument("--ask", action="store_true", help="today's card, marked as a test")
    a = parser.parse_args(argv)
    return {
        "run": cmd_run,
        "check": cmd_check,
        "predict": cmd_predict,
        "import-apple-health": cmd_import,
        "say": cmd_say,
    }[a.cmd](a)


def _profile(data: Path):
    from supercycletracker.bot import load_profile, profile_errors

    try:
        profile = load_profile(data)
    except FileNotFoundError:
        return None, ["missing_profile"]
    except (ValueError, TypeError, yaml.YAMLError):
        return None, ["bad_profile"]  # the parser's message may quote a value
    return profile, profile_errors(profile, data)


def cmd_check(a) -> int:
    """`profile.yaml` and every log line; exit 1 on errors. A `cycle_start` flag that differs
    from the 10-day rule is reported, not an error: the bot re-derives the flags on its next
    write."""
    errors: list[str] = []
    _, problems = _profile(a.data)
    errors += [f"profile: {code}" for code in problems]

    path = a.data / LOG_FILE
    log: records.Log = {}
    lines: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            errors.append(f"log line {n}: bad_json")  # json's message may quote the line
            continue
        try:
            day = records.Day.from_dict(obj)
        except ValueError as e:  # a reason code, never the value
            errors.append(f"log line {n}: {e}")
            continue
        if day.date in log:
            errors.append(f"log line {n}: duplicate_date")
            continue
        log[day.date], lines[day.date] = day, n
    derived = records.derive_starts(log)
    for d in sorted(log):
        if derived[d].cycle_start != log[d].cycle_start:
            print(f"log line {lines[d]}: cycle_start differs from the 10-day rule")

    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    starts = records.starts(log)
    print("profile ok")
    print(f"log ok: {len(log)} days, {len(starts)} starts")
    return 0


def cmd_predict(a) -> int:
    profile, problems = _profile(a.data)
    try:
        log = records.load(a.data / LOG_FILE)
    except ValueError as e:
        print(f"log: {e}", file=sys.stderr)
        return 1
    tz = profile.timezone if profile is not None and "bad_timezone" not in problems else "UTC"
    today = a.today or cycle.bot_day(datetime.now(UTC), tz)
    ph = cycle.phase(log, today)
    p = ph.prediction
    print(f"today: {today.isoformat()}")
    print(f"starts: {len(records.starts({d: x for d, x in log.items() if d <= today}))}")
    if p is None:
        print("prediction: none, the log has no cycle start")
        return 0
    print(f"last start: {p.last_start.isoformat()}")
    print(f"lengths: {', '.join(str(n) for n in p.lengths) or '-'}")
    print(f"median: {p.median}")
    print(f"period days: {p.period_days}")
    print(f"expected: {p.expected.isoformat()}")
    print(f"window: {p.window_start.isoformat()} .. {p.window_end.isoformat()}")
    print(f"phase: {ph.kind}" + (f" day {ph.day}" if ph.day else ""))
    return 0


def cmd_import(a) -> int:
    """Dates follow the profile timezone, so the import refuses to guess one: without a
    readable profile with a valid timezone a record near midnight would land on another day."""
    profile, problems = _profile(a.data)
    blocking = [c for c in problems if c in ("missing_profile", "bad_profile", "bad_timezone")]
    if profile is None or blocking:
        for code in blocking:
            print(f"profile: {code}", file=sys.stderr)
        return 1
    try:
        report = applehealth.merge(a.export, a.data, profile.timezone, dry_run=a.dry_run)
    except applehealth.ImportFailed as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    print(report.text(dry_run=a.dry_run))
    return 0


def cmd_say(a) -> int:
    """`--ask`: today's card marked as a test, without touching `last_ask`: the positive
    control for a live deployment. It does not poll, so it is safe beside the running bot."""
    if not a.ask:
        print("say: nothing to send (use --ask)", file=sys.stderr)
        return 2
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is empty", file=sys.stderr)
        return 2
    from supercycletracker.bot import Bot
    from supercycletracker.telegram import Telegram, TelegramError, safe

    profile, problems = _profile(a.data)
    if problems:
        print("profile: " + ", ".join(problems), file=sys.stderr)
        return 1
    bot = Bot(a.data, Telegram(token), profile=profile)
    try:
        bot.send_card(bot.chat_id, bot.today(), " " + bot.t("test_mark"))
    except (TelegramError, ValueError) as e:
        print(f"say failed: {safe(e)}", file=sys.stderr)
        return 1
    finally:
        bot.close()
    print("card sent")
    return 0


def cmd_run(a) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log = logging.getLogger("sct")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is empty")
        return 2
    profile, problems = _profile(a.data)
    if problems:
        log.error("profile: %s", ", ".join(problems))
        return 2
    from supercycletracker.bot import Bot, Interrupted
    from supercycletracker.telegram import Telegram, TelegramError, safe

    bot = Bot(
        a.data,
        Telegram(token),
        token=os.environ.get("SCT_GIT_TOKEN", "").strip(),
        profile=profile,
        alive=ALIVE,
    )
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
        if bot.polling:  # a long poll would hold the exit for up to 20 s
            raise Interrupted

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("sct %s", __version__)
    try:
        bot.run(stop)
    except TelegramError as e:
        log.error("stopped: %s", safe(e))
        return 1
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
