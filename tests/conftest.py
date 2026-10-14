import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from supercycletracker import records
from supercycletracker.bot import Bot
from supercycletracker.records import Day
from supercycletracker.telegram import TelegramError

HUMAN = ["-c", "user.name=Someone", "-c", "user.email=someone@example.invalid"]
ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(path: Path, *args: str) -> str:
    """git as a person, cut off from the host's own configuration."""
    r = subprocess.run(
        ["git", "-C", str(path), *HUMAN, *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **ISOLATED},
    )
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


def seed_log() -> dict[date, Day]:
    """Three fictional periods of five, five and three days."""
    log = {}
    for first, n in ((date(2027, 1, 4), 5), (date(2027, 2, 1), 5), (date(2027, 3, 1), 3)):
        for i in range(n):
            d = first + timedelta(days=i)
            log[d] = Day(date=d, source="telegram", flow="medium" if i < 2 else "light")
    return records.derive_starts(log)


@dataclass
class DataRepo:
    remote: Path  # the bare repository both clones push to
    data: Path  # the bot's checkout
    human: Path  # a person's clone

    @property
    def log(self) -> Path:
        return self.data / "cycle" / "log.jsonl"

    def remote_count(self) -> int:
        return int(git(self.remote, "rev-list", "--count", "main"))

    def remote_file(self, path: str) -> str:
        return git(self.remote, "show", f"main:{path}")


@pytest.fixture(scope="session")
def _data_repo_template(tmp_path_factory) -> Path:
    """Built once: git is slow to spawn on a loaded machine, a directory copy is not. The
    clones point at the remote by a relative path, so a copy works anywhere."""
    root = tmp_path_factory.mktemp("template")
    remote, seed = root / "remote.git", root / "seed"
    git(root, "init", "--quiet", "--bare", "-b", "main", str(remote))
    git(root, "init", "--quiet", "-b", "main", str(seed))
    (seed / "cycle").mkdir()
    records.save(seed / "cycle" / "log.jsonl", seed_log())
    (seed / "cycle" / "profile.yaml").write_text("name: Example\nlanguage: es\n", encoding="utf-8")
    (seed / ".gitignore").write_text("state/\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "seed")
    git(seed, "push", "--quiet", str(remote), "main")
    for name in ("data", "human"):
        git(root, "clone", "--quiet", "remote.git", name)
        git(root / name, "remote", "set-url", "origin", "../remote.git")
    shutil.rmtree(seed)
    return root


@pytest.fixture
def data_repo(_data_repo_template, tmp_path, monkeypatch) -> DataRepo:
    """A bare remote, the bot's checkout and a person's clone, with git cut off from the host's
    own configuration so a global hook or signing setting cannot change the result."""
    for key, value in ISOLATED.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "repos"
    shutil.copytree(_data_repo_template, root, symlinks=True)
    return DataRepo(remote=root / "remote.git", data=root / "data", human=root / "human")


class FakeTelegram:
    """Stands in for `Telegram`: records every call, hands out increasing message ids, and
    raises a scripted `TelegramError` on demand. Safe across threads (the worker sends too).

    `member` is what `getChatMember` answers for the bot; `files` maps a `file_id` to the bytes
    `getFile` + `download` hand back (a missing one fails like Telegram does)."""

    def __init__(self, me: dict | None = None):
        self.me = me or {
            "id": 999,
            "is_bot": True,
            "username": "example_cycle_bot",
            "can_read_all_group_messages": False,
        }
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, list[TelegramError]] = {}
        self.updates: list = []  # what each getUpdates returns, in order: a list or a callable
        self.member: dict = {"status": "member"}
        self.files: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._next_id = 1000

    def fail_next(self, method: str, code: int, description: str, parameters=None) -> None:
        self.errors.setdefault(method, []).append(
            TelegramError(method, code, description, parameters)
        )

    def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        with self._lock:
            self.calls.append((method, params))
            pending = self.errors.get(method)
            if pending:
                raise pending.pop(0)
            if method == "getMe":
                return dict(self.me)
            if method == "getChatMember":
                return dict(self.member)
            if method == "getFile":
                file_id = params["file_id"]
                if file_id not in self.files:
                    raise TelegramError(method, 400, "Bad Request: invalid file_id")
                size = len(self.files[file_id])
                return {"file_id": file_id, "file_size": size, "file_path": f"voice/{file_id}.oga"}
            if method == "getUpdates":
                batch = self.updates.pop(0) if self.updates else []
                return batch() if callable(batch) else batch
            if method == "sendMessage":
                self._next_id += 1
                return {
                    "message_id": self._next_id,
                    "chat": {"id": params["chat_id"]},
                    "text": params.get("text", ""),
                }
            return True

    def download(self, file_path: str, limit: int | None = None) -> bytes:
        file_id = file_path.removeprefix("voice/").removesuffix(".oga")
        with self._lock:
            self.calls.append(("download", {"file_path": file_path, "limit": limit}))
            pending = self.errors.get("download")
            if pending:
                raise pending.pop(0)
        data = self.files[file_id]
        if limit is not None and len(data) > limit:
            raise TelegramError("download", 413, "file too large")
        return data

    def of(self, method: str) -> list[dict]:
        with self._lock:
            return [p for m, p in self.calls if m == method]

    def texts(self) -> list[str]:
        return [p["text"] for p in self.of("sendMessage")]


class Clock:
    """The bot's `now`: a fixed aware datetime moved by hand."""

    def __init__(self, start: datetime):
        self.at = start

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


FIXTURES = Path(__file__).parent / "fixtures"
MADRID = ZoneInfo("Europe/Madrid")
GROUP, HER, ADMIN, STRANGER, BOT_ID = -100123, 111, 222, 333, 999

PROFILE = """\
name: Lucía
birth_date: 1993-05-02
timezone: Europe/Madrid
language: es
ask_at: "21:00"
telegram:
  chat_id: -100123
  user_id: 111
  username: lucia_example
  admins: {222: Marta}
assistant:
  plugin: plugins/fake.py
  timeout_s: 5
  model: null
  instructions: ""
"""


def at(day: date, hour: int = 21, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MADRID)


@pytest.fixture
def make_bot(data_repo):
    """A bot on the fictional seed log (periods from 2027-01-04, 02-01 and 03-01, the last one
    with three bleed days and no closing day), at 21:00 on `today`."""
    bots = []

    def make(today=date(2027, 3, 4), profile=PROFILE, log=None, read_all=False, member=None):
        (data_repo.data / "cycle" / "profile.yaml").write_text(profile, encoding="utf-8")
        (data_repo.data / "plugins").mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "fake_plugin.py", data_repo.data / "plugins" / "fake.py")
        if log is not None:
            records.save(data_repo.log, records.derive_starts(log))
        tg = FakeTelegram()
        tg.me["can_read_all_group_messages"] = read_all
        if member is not None:
            tg.member = member
        bot = Bot(data_repo.data, tg, now=Clock(at(today)))
        bot.debounce_s = 0  # a test's burst is sent on purpose; see the debounce tests
        bot.start()
        bots.append(bot)
        return bot

    yield make
    for bot in bots:
        bot.close()
