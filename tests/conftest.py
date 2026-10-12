from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


class Repository:
    def __init__(self, root: Path):
        self.root = root
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Ripple Test",
            GIT_AUTHOR_EMAIL="test@example.invalid",
            GIT_COMMITTER_NAME="Ripple Test",
            GIT_COMMITTER_EMAIL="test@example.invalid",
            GIT_AUTHOR_DATE="2026-10-12T00:00:00+0000",
            GIT_COMMITTER_DATE="2026-10-12T00:00:00+0000",
        )
        self.git("init", "--quiet")

    def git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false", *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout

    def commit(self, files: dict[str, str | bytes | None]) -> None:
        for relative, contents in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if contents is None:
                path.unlink()
            elif isinstance(contents, bytes):
                path.write_bytes(contents)
            else:
                path.write_bytes(contents.encode("utf-8"))
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "-m", "fixture revision")


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path)


@pytest.fixture(autouse=True)
def isolate_user_cache(tmp_path, monkeypatch):
    base = str(tmp_path.parent / "ripple-test-cache")
    monkeypatch.setenv("LOCALAPPDATA", base)
    monkeypatch.setenv("XDG_CACHE_HOME", base)
