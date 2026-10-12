from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ripple.analysis import analyze
from ripple.config import SourceConfig, validate_roots
from ripple.errors import RippleError
from ripple.models import Policy

CONFIG = '[tool.ripple]\nsource_roots = ["lib"]\n'
FILES = {
    "pyproject.toml": CONFIG,
    "lib/pkg/util.py": "def f(): return 1\n",
    "lib/pkg/api.py": "from .util import f\ndef g(): return f()\n",
}


def test_snapshot_toml_source_roots_and_dirty_config_are_immutable(repo):
    repo.commit(FILES)
    repo.commit({"lib/pkg/util.py": "def f(): return 2\n"})
    (repo.root / "pyproject.toml").write_text('[tool.ripple]\nsource_roots = ["MISSING"]\n')
    status = repo.git("status", "--porcelain=v1", "-z")
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert [i.display_name for i in report.impacts] == ["pkg.api.g"]
    assert (
        report.tool["base_config"]
        == report.tool["head_config"]
        == SourceConfig(("lib",)).fingerprint
    )
    assert repo.git("status", "--porcelain=v1", "-z") == status


def test_cli_source_root_override_and_index_metadata(repo):
    repo.commit(
        {
            "code/pkg/util.py": "def f(): pass\n",
            "code/pkg/api.py": "from .util import f\ndef g(): f()\n",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ripple",
            "index",
            "--repo",
            str(repo.root),
            "--source-root",
            "code",
            "--format",
            "json",
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["source_roots"] == ["code"]
    assert "pkg.api.g" in {s["display_name"] for s in data["symbols"]}
    assert data["config_fingerprint"] == SourceConfig(("code",)).fingerprint


@pytest.mark.parametrize(
    "roots",
    [
        [],
        "lib",
        [False],
        ["../outside"],
        ["lib/../other"],
        ["C:/outside"],
        ["/outside"],
        ["lib\\pkg"],
        ["missing"],
        ["lib\n"],
    ],
)
def test_invalid_source_roots_are_config_errors(roots):
    with pytest.raises(RippleError):
        validate_roots(roots, ["lib/pkg/util.py"])


def test_overlapping_roots_use_longest_match_and_sorted_unique_fingerprint():
    roots = validate_roots([".", "src", "src/", "."], ["src/pkg/m.py", "tests/test_m.py"])
    config = SourceConfig(roots)
    assert config.module_for("src/pkg/m.py") == "pkg.m"
    assert config.module_for("tests/test_m.py") == "tests.test_m"
    assert config.fingerprint == SourceConfig(("src", ".")).fingerprint


def test_explicit_roots_exclude_other_sources_visibly(repo):
    repo.commit({**FILES, "elsewhere.py": "def extra(): pass\n"})
    report = analyze(repo.root, "HEAD", "HEAD", Policy())
    assert report.completeness.status == "partial"
    assert "source_root_exclusion" in report.completeness.reasons
    assert report.completeness.skipped_files == 2


@pytest.mark.parametrize(
    "config",
    ["invalid TOML =", "[tool.ripple]\nmax_dept = 3\n", '[tool.ripple]\nsource_roots = "lib"\n'],
)
def test_malformed_or_unknown_config_fails_cleanly(repo, config):
    repo.commit({**FILES, "pyproject.toml": config})
    result = subprocess.run(
        [sys.executable, "-m", "ripple", "index", "--repo", str(repo.root), "--format", "json"],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"RPL-CONFIG-001" in result.stderr


def test_each_revision_uses_its_own_source_root_config(repo):
    repo.commit(FILES)
    repo.commit(
        {
            "pyproject.toml": '[tool.ripple]\nsource_roots = ["."]\n',
            "lib/pkg/util.py": "def f(): return 2\n",
        }
    )
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert report.tool["base_config"] != report.tool["head_config"]
    assert report.to_json() == analyze(repo.root, "HEAD~1", "HEAD", Policy()).to_json()


def test_scope_change_is_not_mislabeled_as_deletion(repo):
    repo.commit({**FILES, "other.py": "def f(): return 1\n"})
    repo.commit({"pyproject.toml": '[tool.ripple]\nsource_roots = ["."]\n'})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert not any(c.display_name == "other.f" for c in report.changes)


def test_symlink_config_blob_is_not_followed(repo):
    repo.commit({"m.py": "def f(): pass\n"})
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            input=b"/outside/private.toml",
            cwd=repo.root,
            env=repo.env,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    repo.git("update-index", "--add", "--cacheinfo", "120000," + blob + ",pyproject.toml")
    repo.git("commit", "--quiet", "-m", "symlink config")
    with pytest.raises(RippleError, match="bounded regular blob"):
        analyze(repo.root, "HEAD", "HEAD", Policy())


def test_excluded_files_do_not_make_existing_root_invalid(repo, monkeypatch):
    repo.commit(FILES)
    monkeypatch.setattr("ripple.git_source.MAX_FILE_BYTES", 1)
    report = analyze(repo.root, "HEAD", "HEAD", Policy())
    assert report.completeness.status == "partial"
    assert "file_size_limit" in report.completeness.reasons
