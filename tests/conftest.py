import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

from supercycletracker import records
from supercycletracker.records import Day

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
