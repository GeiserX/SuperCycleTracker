"""The data directory: the records file, its git sync and undo, and the SQLite plumbing.

`<data>` is a checkout of the household's private data repository and the bot is the only writer
of `cycle/log.jsonl`. Every write and the whole git sequence of `flush` hold `DATA_LOCK`, because
git rewrites the file on a pull and a write landing in the middle would lose one side.

The service log gets event kinds only: never a date of a record, a value or git's own output.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

from supercycletracker import records
from supercycletracker.records import Change, Log

log = logging.getLogger("sct.repo")

DATA_LOCK = threading.RLock()
LOG_FILE = "cycle/log.jsonl"
PROFILE_FILE = "cycle/profile.yaml"
STATE_DB = "state/sct.sqlite"

QUIET_S = 60  # commit once the newest write is this old: a three-tap check-in is one commit
PULL_EVERY_S = 3600  # with nothing to push, pull this often so profile and plugin edits arrive
RETRY_S = 60  # after a failed git step, wait this long: a hung remote must not hold the lock
GIT_TIMEOUT_S = 60

BOT_NAME = "SuperCycleTracker"
BOT_EMAIL = "supercycletracker@localhost"
LOG_PREFIX = "log: "
UNDO_PREFIX = "undo: "
SETTINGS_PREFIX = "settings: "  # a change she asked for from the chat; never undone by /deshacer
DERIVE_PREFIX = "derive: "  # flags moved by a pull; not a check-in, so never undone


# --- SQLite kv -----------------------------------------------------------------------------

KV_KEYS = frozenset({"offset", "last_ask", "migrated_chat", "calplan"})
KV_PREFIXES = ("conv:", "pending:", "skip:", "said:", "options:", "ctx:", "remind:")
CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, ts INTEGER NOT NULL,
  role TEXT NOT NULL, who TEXT, text TEXT NOT NULL, body TEXT, mid INTEGER, asked INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_by_chat ON chat (chat, id);
CREATE TABLE IF NOT EXISTS reminder (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, uid INTEGER, at REAL NOT NULL,
  text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', by INTEGER, created INTEGER NOT NULL
);
"""


class KV:
    """One table `kv(key, value)`. It holds exactly `offset`, `last_ask`, `conv:<chat>`,
    `pending:<chat>`, `said:<chat>` (the last answered messages, so an edit can be compared),
    `options:<chat>` (the choices under recent answers), `remind:<chat>` (reminders and calendar
    entries offered with buttons), `ctx:<chat>` (the last chat log row a turn took as context),
    `calplan` (the calendar entries last offered), `skip:<date>` and `migrated_chat`; any other
    key is refused, so nothing else about the cycle can creep into a second store.

    Two more tables: `chat`, the chat log (every message of the chats the bot serves that it
    keeps, and its answers, for the assistant's recap), and `reminder`, the one-off reminders set
    from the chat. Safe across threads."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))  # her words sit here
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._db.executescript(CHAT_SCHEMA)

    @staticmethod
    def _check(key: str) -> None:
        if key not in KV_KEYS and not key.startswith(KV_PREFIXES):
            raise ValueError("unknown_kv_key")

    def get(self, key: str, default: str | None = None) -> str | None:
        self._check(key)
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self._check(key)
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def delete(self, key: str) -> None:
        self._check(key)
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    def keys(self, prefix: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key", (len(prefix), prefix)
            ).fetchall()
        return [r[0] for r in rows]

    def get_json(self, key: str, default=None):
        raw = self.get(key)
        return default if raw is None else json.loads(raw)

    def set_json(self, key: str, value) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False))

    def update_json(self, key: str, fn: Callable, default=None):
        """Read, change and write one value as a single step; `fn` returning None deletes it."""
        self._check(key)
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            value = fn(json.loads(row[0]) if row else default)
            if value is None:
                self._db.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                self._db.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return value

    # --- the chat log ------------------------------------------------------------------

    def add_chat(
        self,
        chat: int,
        role: str,
        who: str | None,
        text: str,
        ts: int,
        *,
        body: str | None = None,
        mid: int | None = None,
        asked: bool = True,
    ) -> int:
        """Log one line: `role` user or assistant; `asked` False for a group message that did
        not call the bot. Returns its row id."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO chat (chat, ts, role, who, text, body, mid, asked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat, int(ts), role, who, text, body, mid, int(bool(asked))),
            )
            return int(cur.lastrowid)

    def chat_rows(self, chat: int, since: int, upto: int | None = None) -> list[dict]:
        """The chat's rows written at `since` (epoch seconds) or later, with a row id up to
        `upto`, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, role, who, text, body, mid, asked FROM chat "
                "WHERE chat = ? AND ts >= ? AND id <= ? ORDER BY id",
                (chat, int(since), upto if upto is not None else 2**62),
            ).fetchall()
        keys = ("id", "ts", "role", "who", "text", "body", "mid", "asked")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def chat_last(self, chat: int) -> int:
        """The newest row id of the chat's log, 0 without one."""
        with self._lock:
            row = self._db.execute("SELECT MAX(id) FROM chat WHERE chat = ?", (chat,)).fetchone()
        return int(row[0] or 0)

    def chat_find(self, chat: int, mid: int) -> dict | None:
        """The newest user row of a Telegram message, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, text, body, asked FROM chat WHERE chat = ? AND mid = ? AND role = "
                "'user' ORDER BY id DESC LIMIT 1",
                (chat, mid),
            ).fetchone()
        return dict(zip(("id", "text", "body", "asked"), row, strict=True)) if row else None

    def chat_edit(self, row: int, text: str, body: str, asked: bool | None = None) -> None:
        """An edited message keeps its row, so later turns and recaps see its final wording."""
        with self._lock:
            if asked is None:
                self._db.execute(
                    "UPDATE chat SET text = ?, body = ? WHERE id = ?", (text, body, row)
                )
            else:
                self._db.execute(
                    "UPDATE chat SET text = ?, body = ?, asked = ? WHERE id = ?",
                    (text, body, int(asked), row),
                )

    def chat_clear(self, chat: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM chat WHERE chat = ?", (chat,))

    def chat_prune(self, before: int) -> int:
        """Drop every row written before `before` (epoch seconds); returns how many."""
        with self._lock:
            return self._db.execute("DELETE FROM chat WHERE ts < ?", (int(before),)).rowcount

    # --- one-off reminders ------------------------------------------------------------

    def add_reminder(self, chat: int, uid: int | None, at: float, text: str, by, now: int) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminder (chat, uid, at, text, by, created) VALUES (?, ?, ?, ?, ?, ?)",
                (chat, uid, float(at), text[:500], by, int(now)),
            )
            return int(cur.lastrowid)

    def reminders(self, chat: int | None = None, state: str = "pending") -> list[dict]:
        """The reminders in `state` (of one chat, or every chat), soonest first."""
        q, args = "SELECT id, chat, uid, at, text, state FROM reminder WHERE state = ?", [state]
        if chat is not None:
            q += " AND chat = ?"
            args.append(chat)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY at, id", args).fetchall()
        keys = ("id", "chat", "uid", "at", "text", "state")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def due_reminders(self, now: float) -> list[dict]:
        return [r for r in self.reminders() if r["at"] <= now]

    def settle_reminder(self, rid: int, state: str) -> bool:
        """Move a pending reminder to `sent`, `cancelled` or `failed`; False when it was no
        longer pending, so two taps (or a tap and the clock) settle it once."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE reminder SET state = ? WHERE id = ? AND state = 'pending'", (state, rid)
            )
            return cur.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()


# --- git -----------------------------------------------------------------------------------


class GitError(RuntimeError):
    """A failed git step. The message is a reason code, never git's output."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def git_env(token: str = "") -> dict[str, str]:
    """The environment for one git call. The token travels as an `http.extraHeader` in the
    environment, never on the command line, where any process could read it, nor in the
    checkout's config. Commits are authored by the bot."""
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME=BOT_NAME,
        GIT_AUTHOR_EMAIL=BOT_EMAIL,
        GIT_COMMITTER_NAME=BOT_NAME,
        GIT_COMMITTER_EMAIL=BOT_EMAIL,
        GIT_TERMINAL_PROMPT="0",
    )
    if token:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        env["GIT_CONFIG_COUNT"] = str(n + 1)
        env[f"GIT_CONFIG_KEY_{n}"] = "http.extraHeader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"Authorization: token {token}"
    return env


def _changed(old: Log, new: Log) -> list[date]:
    """Dates whose line differs between two logs, oldest first."""
    return sorted(
        d
        for d in set(old) | set(new)
        if (old[d].to_line() if d in old else None) != (new[d].to_line() if d in new else None)
    )


def _replace_text(path: Path, text: str) -> None:
    """Write `text` to `path` through a temp file and `os.replace`, keeping its mode."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def _dates_text(dates: list[date]) -> str:
    return ", ".join(d.isoformat() for d in dates) or "sync"


class Repo:
    def __init__(
        self, data: str | Path, token: str = "", clock: Callable[[], float] = time.monotonic
    ):
        self.data = Path(data)
        self.log_path = self.data / LOG_FILE
        self.profile_path = self.data / PROFILE_FILE
        self.profile_pulled = False  # a pull changed profile.yaml: the bot reloads it, then clears
        self.token = token
        self.clock = clock
        self.remote_ok: bool | None = None  # the startup `push --dry-run`, kept for /estado
        self.last_error: str | None = None
        self._last_write: float | None = None
        self._last_pull: float | None = None
        self._retry_at: float | None = None

    # --- the records file ----------------------------------------------------------------

    def load(self) -> Log:
        with DATA_LOCK:
            return records.load(self.log_path)

    def write(self, fn: Callable[[Log], Change]) -> Change:
        """Load, let `fn` build the `Change`, re-derive the starts, save atomically. A `Change`
        that leaves the log as it was writes nothing.

        A log that does not parse (a bad hand edit pulled from the remote) raises `ValueError`
        with a reason code and nothing is written; the caller answers with a toast."""
        with DATA_LOCK:
            before = records.load(self.log_path)
            change = fn(before)
            after = records.derive_starts(change.after)
            if after != dict(change.after):
                change = replace(change, after=after)
            if after != before:
                records.save(self.log_path, after)
                self._last_write = self.clock()
            return change

    # --- git plumbing --------------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.data), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=git_env(self.token),
            check=False,
        )

    def _run(self, *args: str, code: str) -> str:
        r = self._git(*args)
        if r.returncode != 0:
            raise GitError(code)
        return r.stdout

    def _show(self, rev: str) -> str | None:
        """The log's text at `rev`, or None when that revision has no log."""
        r = self._git("show", f"{rev}:./{LOG_FILE}")  # `./`: the data may be a folder of the repo
        return r.stdout if r.returncode == 0 else None

    def _dirty(self) -> bool:
        return bool(self._run("status", "--porcelain", "--", LOG_FILE, code="status_failed"))

    def _commit(self, message: str, path: str = LOG_FILE) -> None:
        """Commit the log (or `path`) and only that, whatever else a person left staged."""
        self._run("commit", "--quiet", "-m", message, "--", path, code="commit_failed")

    def _abort_rebase(self) -> None:
        try:
            self._git("rebase", "--abort")  # fails harmlessly when no rebase is in progress
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _read(self, path: Path | None = None) -> bytes | None:
        try:
            return (path or self.log_path).read_bytes()
        except FileNotFoundError:
            return None

    def check_remote(self) -> bool:
        """`git push --dry-run` at start; the answer is kept in memory for /estado."""
        try:
            self.remote_ok = self._git("push", "--dry-run", "--quiet").returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            self.remote_ok = False
        log.info("git push --dry-run %s", "ok" if self.remote_ok else "failed")
        return self.remote_ok

    def unpushed(self) -> int | None:
        """Commits on this branch that the remote does not have; None without an upstream."""
        r = self._git("rev-list", "--count", "@{u}..HEAD")
        return int(r.stdout) if r.returncode == 0 else None

    # --- sync ----------------------------------------------------------------------------

    def flush(self, force: bool = False) -> str:
        """Commit the log after 60 s of quiet (at once with `force`, on SIGTERM), then pull
        with rebase and push. With nothing to push, pull once an hour. Returns the event kind:
        `waiting`, `pushed`, `pulled`, `idle`, `backoff` or `failed`.

        A failure keeps the commit, sets `last_error` to a reason code and backs off for
        `RETRY_S` (`backoff`), so a hung remote does not hold `DATA_LOCK` on every tick;
        `force` ignores the backoff."""
        with DATA_LOCK:
            now = self.clock()
            if not force and self._retry_at is not None and now < self._retry_at:
                return "backoff"
            try:
                result = self._flush(now, force)
            except (GitError, subprocess.TimeoutExpired, OSError) as e:
                self.last_error = e.code if isinstance(e, GitError) else type(e).__name__
                self._retry_at = now + RETRY_S
                log.warning("git sync failed: %s", self.last_error)
                return "failed"
            self.last_error = None
            self._retry_at = None
            return result

    def _flush(self, now: float, force: bool) -> str:
        committed = False
        if self._dirty():
            if not force and self._last_write is not None and now - self._last_write < QUIET_S:
                return "waiting"
            self._commit_log()
            committed = True
        if committed or self.unpushed():
            self._pull()
            self._run("push", "--quiet", code="push_failed")
            log.info("git push ok")
            return "pushed"
        if self._last_pull is None or now - self._last_pull >= PULL_EVERY_S:
            self._pull()
            return "pulled"
        return "idle"

    def _commit_log(self) -> None:
        head = self._show("HEAD")
        try:
            dates = _changed(records.loads(head or ""), records.load(self.log_path))
        except ValueError:
            dates = []
        self._run("add", "--", LOG_FILE, code="add_failed")
        self._commit(LOG_PREFIX + _dates_text(dates))
        self._last_write = None
        log.info("git commit ok days=%d", len(dates))

    def _pull(self) -> None:
        """`pull --rebase -X theirs`: in a rebase "theirs" is the commit being replayed, the
        bot's, so a conflicting hunk in the log resolves to the bot's side. git resolves by
        hunk, not by line, so a person's edit to a day right next to one the bot changed is in
        the same hunk and goes too; edits further away survive.

        `--autostash` covers a person's uncommitted edit to another tracked file in the
        checkout (the log itself is never dirty here: `_flush` commits it or waits first).

        A failure aborts the rebase and keeps the bot's commit. A conflict `-X theirs` cannot
        settle (a person deleted the log while the bot had a commit to push) fails as
        `pull_conflict` on every try until a person restores the file on the remote."""
        before, profile = self._read(), self._read(self.profile_path)
        try:
            r = self._git("pull", "--rebase", "-X", "theirs", "--autostash", "--quiet")
        except subprocess.TimeoutExpired:
            self._abort_rebase()  # killed mid-rebase would leave HEAD detached
            raise
        self._last_pull = self.clock()
        if r.returncode != 0:
            try:
                conflict = self._git("diff", "--name-only", "--diff-filter=U").stdout.strip()
            finally:
                self._abort_rebase()  # even when the conflict probe itself times out
            raise GitError("pull_conflict" if conflict else "pull_failed")
        if self._read() != before:
            self._rederive()
        if self._read(self.profile_path) != profile:
            self.profile_pulled = True
        log.info("git pull ok")

    def _rederive(self) -> None:
        """After a pull that changed the file: re-run the 10-day rule and, if a flag moves,
        commit it at once as `derive: <dates>`, which is not a check-in, so /deshacer leaves it
        alone. A log that does not parse is logged with its reason code and left as it is;
        writes refuse it, so the next pull, with a person's fix, comes in `RETRY_S`, not an hour."""
        try:
            current = records.load(self.log_path)
        except ValueError as e:
            log.warning("log unreadable after pull: %s", e)
            self._last_pull = self.clock() - PULL_EVERY_S + RETRY_S
            return
        derived = records.derive_starts(current)
        if derived != current:
            records.save(self.log_path, derived)
            self._commit(DERIVE_PREFIX + _dates_text(_changed(current, derived)))
            log.info("starts re-derived after pull")

    # --- the profile ---------------------------------------------------------------------

    def write_profile(self, fn: Callable[[str], str], message: str) -> str:
        """Rewrite `profile.yaml` with `fn` (old text to new text, raising `ValueError` to
        refuse) and commit it alone as `settings: <message>`; the next `flush` pushes it.
        Returns the new text. A commit that fails puts the old text back and raises."""
        with DATA_LOCK:
            old = self.profile_path.read_text(encoding="utf-8")
            new = fn(old)
            if new == old:
                return old
            _replace_text(self.profile_path, new)
            try:
                self._run("add", "--", PROFILE_FILE, code="add_failed")
                self._commit(SETTINGS_PREFIX + message, PROFILE_FILE)
            except (GitError, subprocess.TimeoutExpired, OSError):
                _replace_text(self.profile_path, old)
                self._git("reset", "--quiet", "--", PROFILE_FILE)
                raise
            log.info("git commit ok: settings")
            return new

    # --- undo ----------------------------------------------------------------------------

    def undo(self) -> Change | None:
        """Undo the last check-in. While the log is dirty, restore it from HEAD (the whole
        uncommitted batch goes, nothing is committed). When it is clean and the last commit is
        the bot's `log:` commit, restore it from that commit's parent and commit `undo:`.
        Otherwise None. With the data in a folder of a bigger repo, the last commit is the last
        one that touched the folder, so another folder's commit on top does not hide it.

        Returns a `Change` from the current log to the restored one: the dates that went back.
        """
        with DATA_LOCK:
            current = self._read()
            if self._dirty():
                rev, message = "HEAD", None
            else:
                scope = () if (self.data / ".git").exists() else ("--", ".")
                head = self._run("log", "-1", "--format=%H%x00%ae%x00%s", *scope, code="log_failed")
                sha, email, subject = head.rstrip("\n").split("\x00", 2)
                if email != BOT_EMAIL or not subject.startswith(LOG_PREFIX):
                    return None
                rev, message = f"{sha}~1", UNDO_PREFIX + subject.removeprefix(LOG_PREFIX)
            target = self._show(rev)
            if target is not None:
                self._run("checkout", rev, "--", LOG_FILE, code="checkout_failed")
            elif message is None:
                self.log_path.unlink(missing_ok=True)  # never committed: nothing to go back to
            else:
                self._run("rm", "--quiet", "--", LOG_FILE, code="rm_failed")
            if message is not None:
                self._commit(message)
            self._last_write = None
            log.info("undo %s", "committed" if message else "restored")
            try:
                before = records.loads((current or b"").decode("utf-8"))
                after = records.loads(target or "")
            except ValueError:
                return Change(before={}, after={}, items=())
            items = tuple((d, before.get(d), after.get(d)) for d in _changed(before, after))
            return Change(before=before, after=after, items=items)
