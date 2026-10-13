import os
import stat
import subprocess
import threading
from datetime import date

import pytest
from conftest import git

from supercycletracker import records
from supercycletracker.records import Edit
from supercycletracker.repo import BOT_EMAIL, DATA_LOCK, KV, LOG_FILE, Repo, git_env

DAY = date(2027, 3, 4)
TOKEN = "tok-0123456789-not-a-real-one"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def tap(repo: Repo, d: date = DAY, source: str = "telegram", **fields):
    return repo.write(lambda log: records.apply(log, Edit(days={d: fields}), source))


def subject(path, rev="HEAD") -> str:
    return git(path, "log", "-1", "--format=%s", rev).strip()


def human_edit(dr, fn, message="edit by hand"):
    """A person changes the log in their own clone and pushes."""
    git(dr.human, "pull", "--quiet")
    path = dr.human / LOG_FILE
    log = records.load(path)
    records.save(path, fn(log))
    git(dr.human, "commit", "--quiet", "-am", message)
    git(dr.human, "push", "--quiet")


def human_profile(dr, text):
    git(dr.human, "pull", "--quiet")
    (dr.human / "cycle" / "profile.yaml").write_text(text, encoding="utf-8")
    git(dr.human, "commit", "--quiet", "-am", "profile")
    git(dr.human, "push", "--quiet")


# --- commit and push -----------------------------------------------------------------------


def test_three_taps_become_one_commit_after_60s_of_quiet(data_repo):
    dr, clock = data_repo, Clock()
    repo, n0 = Repo(dr.data, clock=clock), data_repo.remote_count()
    assert tap(repo, flow="medium")
    clock.advance(10)
    assert tap(repo, pain=4)
    clock.advance(10)
    assert tap(repo, symptoms=["headache"])
    clock.advance(59)
    assert repo.flush() == "waiting"
    assert dr.remote_count() == n0
    clock.advance(1)
    assert repo.flush() == "pushed"
    assert dr.remote_count() == n0 + 1
    assert subject(dr.remote, "main") == "log: 2027-03-04"
    assert git(dr.remote, "log", "-1", "--format=%ae", "main").strip() == BOT_EMAIL
    assert dr.remote_file(LOG_FILE) == dr.log.read_text(encoding="utf-8")
    assert repo.flush() == "idle"


def test_a_tap_that_changes_nothing_writes_nothing(data_repo):
    repo = Repo(data_repo.data, clock=Clock())
    tap(repo, flow="medium")
    assert repo.flush(force=True) == "pushed"
    assert not tap(repo, flow="medium")
    assert git(data_repo.data, "status", "--porcelain") == ""


def test_sigterm_flush_commits_at_once(data_repo):
    repo, n0 = Repo(data_repo.data, clock=Clock()), data_repo.remote_count()
    tap(repo, flow="light")
    assert repo.flush(force=True) == "pushed"
    assert data_repo.remote_count() == n0 + 1


def test_a_profile_pushed_between_commit_and_push_is_rebased_over(data_repo):
    dr, clock = data_repo, Clock()
    repo, n0 = Repo(dr.data, clock=clock), dr.remote_count()
    tap(repo, flow="heavy")
    human_profile(dr, "name: Example\nlanguage: en\n")
    clock.advance(61)
    assert repo.flush() == "pushed"
    assert dr.remote_count() == n0 + 2
    assert git(dr.remote, "rev-list", "--merges", "--count", "main").strip() == "0"
    assert dr.remote_file("cycle/profile.yaml") == "name: Example\nlanguage: en\n"
    assert '"date": "2027-03-04", "flow": "heavy"' in dr.remote_file(LOG_FILE)
    assert (dr.data / "cycle" / "profile.yaml").read_text() == "name: Example\nlanguage: en\n"


def test_a_conflicting_line_resolves_to_the_bots_and_other_edits_survive(data_repo):
    dr, clock = data_repo, Clock()
    repo = Repo(dr.data, clock=clock)
    first, far = date(2027, 1, 4), date(2027, 2, 5)
    tap(repo, d=first, pain=5)

    def by_hand(log):
        log = dict(log)
        log[first] = records.Day(**{**log[first].__dict__, "pain": 2})
        log[far] = records.Day(**{**log[far].__dict__, "note": "edited by hand"})
        return log

    human_edit(dr, by_hand)
    clock.advance(61)
    assert repo.flush() == "pushed"
    remote = records.loads(dr.remote_file(LOG_FILE))
    assert remote[first].pain == 5  # the bot's line
    assert remote[far].note == "edited by hand"  # the person's other edit
    assert dr.log.read_text(encoding="utf-8") == dr.remote_file(LOG_FILE)
    assert repo.unpushed() == 0


def test_starts_are_rederived_after_a_pull_that_changed_the_log(data_repo):
    dr = data_repo
    repo = Repo(dr.data, clock=Clock())
    # By hand: a bleed day five days before the February start, with no flag on it and the
    # February flag left in place, which the 10-day rule says is wrong.
    path = dr.human / LOG_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    added = '{"date": "2027-01-27", "flow": "light", "source": "telegram"}\n'
    at = next(i for i, line in enumerate(lines) if '"2027-02-01"' in line)
    path.write_text("".join(lines[:at] + [added] + lines[at:]), encoding="utf-8")
    git(dr.human, "commit", "--quiet", "-am", "add a day by hand")
    git(dr.human, "push", "--quiet")

    assert repo.flush() == "pulled"
    log = records.load(dr.log)
    assert log[date(2027, 1, 27)].cycle_start and not log[date(2027, 2, 1)].cycle_start
    assert repo.flush() == "pushed"  # the re-derived flags are committed at once
    assert subject(dr.remote, "main") == "log: 2027-01-27, 2027-02-01"


def test_pulls_once_an_hour_when_clean(data_repo):
    dr, clock = data_repo, Clock()
    repo = Repo(dr.data, clock=clock)
    profile = dr.data / "cycle" / "profile.yaml"
    assert repo.flush() == "pulled"
    human_profile(dr, "name: Example\nlanguage: en\n")
    clock.advance(600)
    assert repo.flush() == "idle"
    assert "language: es" in profile.read_text()
    clock.advance(3000)
    assert repo.flush() == "pulled"
    assert "language: en" in profile.read_text()


def test_a_refused_push_keeps_the_commit_and_the_next_flush_pushes_it(data_repo):
    dr, clock = data_repo, Clock()
    repo, n0 = Repo(dr.data, clock=clock), dr.remote_count()
    hook = dr.remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    tap(repo, flow="medium")
    assert repo.flush(force=True) == "failed"
    assert repo.last_error == "push_failed"
    assert repo.unpushed() == 1  # what /estado counts
    assert dr.remote_count() == n0
    assert subject(dr.data) == "log: 2027-03-04"
    assert repo.flush() == "backoff"

    hook.unlink()
    clock.advance(61)
    assert repo.flush() == "pushed"
    assert repo.unpushed() == 0
    assert dr.remote_count() == n0 + 1
    assert repo.last_error is None


def test_data_lock_is_held_for_every_git_call_of_a_flush(data_repo, monkeypatch):
    repo = Repo(data_repo.data, clock=Clock())
    seen: list[bool] = []
    real = repo._git

    def free() -> bool:
        out = []

        def probe():
            got = DATA_LOCK.acquire(blocking=False)
            if got:
                DATA_LOCK.release()
            out.append(got)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return out[0]

    def watched(*args):
        seen.append(free())
        return real(*args)

    assert free()  # positive control: outside a flush another thread gets the lock
    monkeypatch.setattr(repo, "_git", watched)
    tap(repo, flow="light")
    assert repo.flush(force=True) == "pushed"
    assert len(seen) >= 5 and not any(seen)


# --- undo ----------------------------------------------------------------------------------


def test_undo_while_dirty_restores_head_and_commits_nothing(data_repo):
    dr = data_repo
    repo, head = Repo(dr.data, clock=Clock()), git(dr.data, "rev-parse", "HEAD")
    tap(repo, flow="medium")
    tap(repo, d=date(2027, 3, 3), pain=6)
    change = repo.undo()
    assert change.dates == (date(2027, 3, 3), DAY)
    assert change.items[1][1] is not None and change.items[1][2] is None
    assert git(dr.data, "status", "--porcelain") == ""
    assert git(dr.data, "rev-parse", "HEAD") == head


def test_undo_on_a_clean_log_commit_makes_an_undo_commit(data_repo):
    dr = data_repo
    repo = Repo(dr.data, clock=Clock())
    seed = dr.log.read_text(encoding="utf-8")
    tap(repo, flow="medium", pain=3)
    assert repo.flush(force=True) == "pushed"
    change = repo.undo()
    assert change.dates == (DAY,)
    assert subject(dr.data) == "undo: 2027-03-04"
    assert dr.log.read_text(encoding="utf-8") == seed
    assert repo.flush(force=True) == "pushed"
    assert subject(dr.remote, "main") == "undo: 2027-03-04"


def test_undo_on_an_undo_or_a_persons_commit_has_nothing_to_undo(data_repo):
    dr = data_repo
    repo = Repo(dr.data, clock=Clock())
    assert repo.undo() is None  # HEAD is the seed, a person's commit
    tap(repo, flow="medium")
    repo.flush(force=True)
    assert repo.undo()
    assert repo.undo() is None  # HEAD is `undo:`
    assert subject(dr.data) == "undo: 2027-03-04"


def test_commit_messages_carry_dates_only(data_repo):
    dr = data_repo
    repo = Repo(dr.data, clock=Clock())
    tap(repo, flow="heavy", pain=7, symptoms=["abdominal_cramps", "nausea"], note="planted note")
    repo.flush(force=True)
    repo.undo()
    repo.flush(force=True)
    messages = git(dr.remote, "log", "--format=%B", "main")
    assert "log: 2027-03-04" in messages and "undo: 2027-03-04" in messages  # control
    for word in (*records.FLOWS, *records.SYMPTOMS, "pain", "planted"):
        assert word not in messages


# --- token and remote ----------------------------------------------------------------------


def test_the_token_never_lands_in_git_config(data_repo):
    dr = data_repo
    repo = Repo(dr.data, token=TOKEN, clock=Clock())
    tap(repo, flow="light")
    assert repo.flush(force=True) == "pushed"
    assert repo.check_remote()
    assert TOKEN not in (dr.data / ".git" / "config").read_text()
    assert TOKEN not in git(dr.data, "config", "--list")
    # Positive control: the environment git_env builds really carries the header to git.
    header = subprocess.run(
        ["git", "-C", str(dr.data), "config", "--get", "http.extraHeader"],
        env=git_env(TOKEN),
        capture_output=True,
        text=True,
        check=False,
    )
    assert header.stdout.strip() == f"Authorization: token {TOKEN}"


def test_git_env_appends_to_existing_config_entries(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.quotePath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    env = git_env(TOKEN)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "core.quotePath"
    assert env["GIT_CONFIG_KEY_1"] == "http.extraHeader"
    assert git_env("")["GIT_CONFIG_COUNT"] == "1"  # no token, no header


def test_check_remote_reports_an_unreachable_remote(data_repo, tmp_path):
    repo = Repo(data_repo.data, clock=Clock())
    assert repo.check_remote() is True
    git(data_repo.data, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    assert repo.check_remote() is False
    assert repo.remote_ok is False


# --- SQLite kv -----------------------------------------------------------------------------


def test_kv_holds_only_its_keys(tmp_path):
    kv = KV(tmp_path / "state" / "sct.sqlite")
    for key in ("offset", "last_ask", "migrated_chat", "conv:-1", "pending:-1", "skip:2027-01-01"):
        kv.set(key, "1")
        assert kv.get(key) == "1"
    for key in ("phase", "cycle", "convx", "pending"):
        with pytest.raises(ValueError, match="unknown_kv_key"):
            kv.set(key, "1")
    assert kv.keys("skip:") == ["skip:2027-01-01"]
    kv.delete("offset")
    assert kv.get("offset") is None
    assert kv.get("offset", "0") == "0"


def test_kv_json_and_update(tmp_path):
    kv = KV(tmp_path / "sct.sqlite")
    kv.set_json("pending:-5", [{"id": 1, "content": "ñ"}])
    assert kv.get_json("pending:-5") == [{"id": 1, "content": "ñ"}]
    assert kv.update_json("pending:-5", lambda v: [*v, {"id": 2}]) == [
        {"id": 1, "content": "ñ"},
        {"id": 2},
    ]
    assert kv.update_json("pending:-5", lambda v: None) is None
    assert kv.get("pending:-5") is None
    assert kv.get_json("conv:-5", "none") == "none"


def test_kv_works_across_threads_and_is_private(tmp_path):
    path = tmp_path / "state" / "sct.sqlite"
    kv = KV(path)

    def worker(n):
        for i in range(50):
            kv.update_json("pending:-1", lambda v, x=n * 100 + i: [*v, x], default=[])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(kv.get_json("pending:-1")) == 200
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    kv.close()
    assert KV(path).get_json("pending:-1")[0] is not None  # it persists
