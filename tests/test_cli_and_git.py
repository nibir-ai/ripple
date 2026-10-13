from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ripple.errors import RippleError
from ripple.git_source import GitSource, run_git
from ripple.output import safe_text


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "ripple", *map(str, args)], capture_output=True, timeout=30
    )


def test_help_and_version():
    assert cli("--version").stdout.decode().startswith("ripple 0.1.0.dev5")
    help_result = cli("--help")
    assert help_result.returncode == 0
    assert "uncommitted changes are excluded" in help_result.stdout.decode()


def test_cli_json_stdout_and_strict_partial(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(service): service.send()\n"})
    repo.commit({"m.py": "def f(): return 2\ndef g(service): service.send()\n"})
    result = cli("analyze", "--repo", repo.root, "--format", "json", "--strict")
    assert result.returncode == 4
    report = json.loads(result.stdout)
    assert report["completeness"]["unresolved_references"] == 2
    assert result.stderr == b""
    assert result.stdout == cli("analyze", "--repo", repo.root, "--format", "json").stdout


def test_index_json_exposes_unresolved_sites(repo):
    repo.commit({"m.py": "def g(service): service.send()\n"})
    result = cli("index", "--repo", repo.root, "--format", "json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["schema"] == "ripple.index.v1"
    assert data["references"][0]["resolution_state"] == "unresolved"


@pytest.mark.parametrize("extra", [["--base", "BAD_REF"], ["--base=-option"], []])
def test_invalid_or_missing_parent_ref_has_clean_failure(repo, extra):
    repo.commit({"m.py": "def f(): pass\n"})
    result = cli("analyze", "--repo", repo.root, "--format", "json", *extra)
    assert result.returncode == 3
    assert result.stdout == b""
    assert b"RPL-GIT-002" in result.stderr


@pytest.mark.parametrize(
    "flag,value", [("--max-depth", "-1"), ("--max-nodes", "0"), ("--max-depth", "nan")]
)
def test_invalid_limits_are_usage_errors(flag, value):
    result = cli("analyze", flag, value)
    assert result.returncode == 2
    assert result.stdout == b""


def test_outside_git_repo(tmp_path):
    result = cli("index", "--repo", tmp_path)
    assert result.returncode == 3
    assert b"RPL-GIT-001" in result.stderr


def test_tree_oid_is_accepted(repo):
    repo.commit({"m.py": "def f(): pass\n"})
    oid = repo.git("rev-parse", "HEAD^{tree}").decode().strip()
    source = GitSource(repo.root)
    assert source.resolve(oid) == oid


def test_input_guards_and_symlink_blob(repo, monkeypatch):
    repo.commit(
        {"good.py": "def f(): pass\n", "big.py": "#" * 80, ".venv/lib.py": "def f(): pass\n"}
    )
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            input=b"/outside/secret.py",
            cwd=repo.root,
            env=repo.env,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    repo.git("update-index", "--add", "--cacheinfo", "120000," + blob + ",link.py")
    repo.git("commit", "--quiet", "-m", "link blob")
    monkeypatch.setattr("ripple.git_source.MAX_FILE_BYTES", 40)
    source = GitSource(repo.root)
    sources, diagnostics = source.files(source.resolve("HEAD"))
    assert [f.path for f in sources] == ["good.py"]
    assert {d.code for d in diagnostics} == {
        "file_size_limit",
        "default_exclusion",
        "symlink_excluded",
    }


def test_git_output_is_bounded(repo):
    with pytest.raises(RippleError, match="resource limit"):
        run_git(repo.root, ["--version"], limit=3)


def test_inherited_git_directory_does_not_redirect_repo(repo, monkeypatch):
    repo.commit({"m.py": "def f(): pass\n"})
    monkeypatch.setenv("GIT_DIR", str(repo.root / "nonexistent"))
    assert GitSource(repo.root).root.resolve() == repo.root.resolve()


def test_text_escapes_terminal_controls():
    value = safe_text("evil\x1b[31m\n\u202efile.py")
    assert "\x1b" not in value
    assert "\n" not in value
    assert "\u202e" not in value


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain newlines")
def test_nul_delimited_git_paths(repo):
    repo.commit({"odd\nname.py": "def f(): return 1\n"})
    repo.commit({"odd\nname.py": "def f(): return 2\n"})
    result = cli("analyze", "--repo", repo.root, "--format", "json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["changes"][0]["after"]["path"] == "odd\nname.py"
