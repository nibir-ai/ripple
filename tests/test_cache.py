from __future__ import annotations

import ast
import json
import os
import subprocess
import sys

import pytest

from ripple.analysis import analyze
from ripple.cache import SCHEMA_VERSION, Cache, CacheUnavailable
from ripple.cache_codec import ast_data, read_ast
from ripple.git_source import GitSource
from ripple.indexing import IndexStats, index_snapshot, key_for
from ripple.models import Policy


def index(repo, cache, ref="HEAD", **kwargs):
    source = GitSource(repo.root)
    return index_snapshot(source, source.resolve(ref), cache=cache, **kwargs)


def cli(repo, *args):
    return subprocess.run(
        [sys.executable, "-m", "ripple", *args, "--repo", str(repo.root)],
        capture_output=True,
        timeout=30,
    )


def test_warm_snapshot_reuses_no_blobs_parser_or_resolution(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    cache = Cache(repo.root)
    cold = IndexStats()
    first, _ = index(repo, cache, stats=cold)
    warm = IndexStats()
    second, _ = index(repo, cache, stats=warm)
    assert first == second
    assert (cold.blob_reads, cold.parsed_files, cold.resolution_passes) == (1, 1, 1)
    assert (warm.snapshot_hits, warm.blob_reads, warm.parsed_files, warm.resolution_passes) == (
        1,
        0,
        0,
        0,
    )
    assert not warm.warnings


def test_incremental_changed_export_reresolves_unchanged_importer(repo):
    repo.commit(
        {
            "service.py": "def f(): return 1\n",
            "api.py": "from service import f\ndef g(): return f()\n",
        }
    )
    cache = Cache(repo.root)
    index(repo, cache)
    repo.commit({"service.py": "def replacement(): return 2\n"})
    stats = IndexStats()
    incremental, _ = index(repo, cache, stats=stats)
    full, _ = index(repo, None)
    assert incremental == full
    assert stats.syntax_hits == 1 and stats.parsed_files == 1
    assert stats.resolution_passes == 1
    assert not incremental.edges
    assert any(d.code == "unresolved_call" for d in incremental.diagnostics)


@pytest.mark.parametrize(
    "edit",
    [
        {"service.py": None},
        {"service.py": "def f(x=1): return x\n"},
        {"api.py": "from service import f\nf = other\ndef g(): return f()\n"},
        {"pkg/__init__.py": "from service import f\n"},
        {"service.py": "def ???"},
        {"service.py": None, "renamed.py": "def f(): return 1\n"},
    ],
)
def test_edit_sequences_have_full_snapshot_and_report_parity(repo, edit):
    repo.commit(
        {
            "service.py": "def f(): return 1\n",
            "api.py": "from service import f\ndef g(): return f()\n",
            "pkg/__init__.py": "",
        }
    )
    cache = Cache(repo.root)
    index(repo, cache)
    repo.commit(edit)
    incremental, _ = index(repo, cache)
    full, _ = index(repo, None)
    assert incremental == full
    cached_report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    full_report = analyze(repo.root, "HEAD~1", "HEAD", Policy(), use_cache=False)
    forced_report = analyze(repo.root, "HEAD~1", "HEAD", Policy(), force=True)
    assert cached_report.to_json() == full_report.to_json() == forced_report.to_json()


def test_root_config_change_reuses_syntax_but_rebuilds_namespace(repo):
    repo.commit(
        {
            "src/m.py": "def f(): return 1\ndef g(): return f()\n",
            "pyproject.toml": '[tool.ripple]\nsource_roots=["src"]\n',
        }
    )
    cache = Cache(repo.root)
    first, _ = index(repo, cache)
    repo.commit({"pyproject.toml": '[tool.ripple]\nsource_roots=["."]\n'})
    stats = IndexStats()
    result, _ = index(repo, cache, stats=stats)
    full, _ = index(repo, None)
    assert result == full
    assert first.config_fingerprint != result.config_fingerprint
    assert stats.syntax_hits == 1 and stats.parsed_files == 0
    assert all(s.module == "src.m" for s in result.symbols)


def test_force_reparses_without_discarding_prior_state(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    old, _ = index(repo, cache)
    stats = IndexStats()
    new, _ = index(repo, cache, force=True, stats=stats)
    assert old == new
    assert (stats.snapshot_hits, stats.syntax_hits, stats.parsed_files) == (0, 0, 1)
    assert cache.status()["snapshot_count"] == 1


def test_failed_publication_rolls_back_replacement(repo, monkeypatch):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    prior, config = index(repo, cache)
    key = key_for(cache, prior.tree, config)

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("ripple.cache.ast_data", interrupt)
    with pytest.raises(KeyboardInterrupt):
        index(repo, cache, force=True)
    assert cache.get_snapshot(key, prior.tree, config.fingerprint) == prior
    with cache.connection(readonly=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM snapshots WHERE status='staging'").fetchone()[
                0
            ]
            == 0
        )


def test_process_crash_leaves_previous_complete_snapshot(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    prior, config = index(repo, cache)
    script = (
        "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('BEGIN IMMEDIATE'); c.execute('DELETE FROM snapshots'); os._exit(9)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(cache.path)], capture_output=True, timeout=10
    )
    assert result.returncode == 9
    assert (
        cache.get_snapshot(key_for(cache, prior.tree, config), prior.tree, config.fingerprint)
        == prior
    )


def test_corrupt_database_is_quarantined_and_rebuilt(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    cache.path.write_bytes(b"not sqlite")
    repaired = Cache(repo.root)
    assert repaired.repaired
    assert len(list(repaired.directory.glob("index.sqlite3.quarantine.*"))) == 1
    result, _ = index(repo, repaired)
    assert result == index(repo, None)[0]


def test_future_schema_is_never_silently_reused(repo):
    repo.commit({"m.py": "def f(): pass\n"})
    cache = Cache(repo.root)
    with cache.connection() as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(CacheUnavailable, match="incompatible"):
        Cache(repo.root, repair=False)
    repaired = Cache(repo.root)
    assert repaired.repaired
    assert repaired.status()["schema_version"] == SCHEMA_VERSION


def test_v1_migration_preserves_complete_snapshot(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    before, config = index(repo, cache)
    with cache.connection() as connection, connection:
        connection.execute("DROP TABLE syntax")
        connection.execute("DROP TABLE analysis_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version>=2")
        connection.execute("PRAGMA user_version=1")
    migrated = Cache(repo.root)
    assert migrated.repaired is None
    assert (
        migrated.get_snapshot(key_for(cache, before.tree, config), before.tree, config.fingerprint)
        == before
    )
    assert migrated.status()["syntax_count"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE snapshots SET checksum='wrong'",
        "DELETE FROM edges",
        "UPDATE snapshots SET payload='{}'",
    ],
)
def test_payload_or_graph_corruption_rebuilds_with_parity(repo, mutation):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    cache = Cache(repo.root)
    expected, _ = index(repo, cache)
    with cache.connection() as connection, connection:
        connection.execute(mutation)
    assert not cache.doctor()["healthy"]
    stats = IndexStats()
    actual, _ = index(repo, cache, stats=stats)
    assert expected == actual and stats.warnings
    assert cache.doctor()["healthy"]


def test_syntax_corruption_is_reparsed(repo):
    repo.commit({"m.py": "def f(): return 1\n", "n.py": "def g(): return 2\n"})
    cache = Cache(repo.root)
    index(repo, cache)
    with cache.connection() as connection, connection:
        connection.execute("UPDATE syntax SET ast='{}'")
    assert not cache.doctor()["healthy"]
    repo.commit({"new.py": "def h(): return 3\n"})
    stats = IndexStats()
    actual, _ = index(repo, cache, stats=stats)
    assert actual == index(repo, None)[0]
    assert stats.syntax_hits == 0 and stats.parsed_files == 3 and stats.warnings
    assert cache.doctor()["healthy"]


def test_parser_limits_invalidate_exact_snapshot(repo, monkeypatch):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    index(repo, cache)
    monkeypatch.setattr("ripple.frontend.MAX_AST_NODES", 2)
    stats = IndexStats()
    actual, _ = index(repo, cache, stats=stats)
    assert actual == index(repo, None)[0]
    assert stats.snapshot_hits == 0
    assert any(d.code == "ast_node_limit" for d in actual.diagnostics)


def test_version_and_runtime_keys_invalidate_reuse(repo, monkeypatch):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    index(repo, cache)
    monkeypatch.setattr("ripple.cache.ANALYZER", "different-rules")
    stats = IndexStats()
    actual, _ = index(repo, cache, stats=stats)
    assert actual == index(repo, None)[0] and stats.snapshot_hits == 0
    monkeypatch.setattr("ripple.cache.runtime_key", lambda: "different-python")
    stats = IndexStats()
    assert index(repo, cache, stats=stats)[0] == actual
    assert stats.snapshot_hits == 0 and stats.syntax_hits == 0


def test_busy_writer_falls_back_without_mutating_prior_snapshot(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    prior, config = index(repo, cache)
    with cache.connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        stats = IndexStats()
        result, _ = index(repo, cache, force=True, stats=stats)
        assert result == prior and stats.warnings
        writer.rollback()
    assert (
        cache.get_snapshot(key_for(cache, prior.tree, config), prior.tree, config.fingerprint)
        == prior
    )


def test_no_cache_has_no_cache_side_effects_and_same_stdout(repo, tmp_path):
    repo.commit({"m.py": "def f(): return 1\n"})
    repo.commit({"m.py": "def f(): return 2\n"})
    base = tmp_path / "unused-cache"
    full = cli(repo, "analyze", "--no-cache", "--cache-dir", str(base), "--format", "json")
    assert full.returncode == 0 and not base.exists()
    cached = cli(repo, "analyze", "--cache-dir", str(base), "--format", "json")
    warm = cli(repo, "analyze", "--cache-dir", str(base), "--format", "json", "--cache-stats")
    assert full.stdout == cached.stdout == warm.stdout
    assert json.loads(warm.stderr)["cache_stats"]["snapshot_hits"] == 2


def test_status_doctor_clear_are_scoped_and_preserve_source(repo, tmp_path):
    repo.commit({"m.py": "def f(): return 1\n"})
    base = tmp_path / "cache-base"
    unrelated = base / "unrelated.txt"
    base.mkdir()
    unrelated.write_text("keep")
    assert cli(repo, "index", "--cache-dir", str(base)).returncode == 0
    status = json.loads(cli(repo, "status", "--cache-dir", str(base), "--format", "json").stdout)
    assert status["snapshot_count"] == 1 and not status["stale"]
    doctor = cli(repo, "doctor", "--cache-dir", str(base), "--format", "json")
    assert doctor.returncode == 0 and json.loads(doctor.stdout)["healthy"]
    repo.commit({"m.py": "def f(): return 2\n"})
    assert json.loads(cli(repo, "status", "--cache-dir", str(base), "--format", "json").stdout)[
        "stale"
    ]
    before = (repo.root / "m.py").read_bytes()
    assert cli(repo, "cache", "clear", "--cache-dir", str(base)).returncode == 0
    assert Cache(repo.root, base, repair=False).status()["snapshot_count"] == 0
    assert Cache(repo.root, base, repair=False).status()["syntax_count"] == 0
    assert (repo.root / "m.py").read_bytes() == before and unrelated.read_text() == "keep"


def test_read_only_inspection_does_not_create_cache(repo, tmp_path):
    repo.commit({"m.py": "def f(): pass\n"})
    base = tmp_path / "missing"
    for command in ("status", "doctor"):
        result = cli(repo, command, "--cache-dir", str(base), "--format", "json")
        assert result.returncode == 0 and not json.loads(result.stdout)["cache_exists"]
    assert not base.exists()


def test_unowned_or_wrong_owner_database_is_never_replaced(repo):
    repo.commit({"m.py": "def f(): pass\n"})
    cache = Cache(repo.root)
    original = cache.path.read_bytes()
    cache.marker.unlink()
    with pytest.raises(CacheUnavailable, match="unowned"):
        Cache(repo.root)
    assert cache.path.read_bytes() == original
    cache.marker.write_text('{"workspace":"someone else"}')
    with pytest.raises(CacheUnavailable, match="ownership"):
        Cache(repo.root)
    assert cache.path.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="Creating test symlinks requires Windows privileges")
def test_cache_symlink_is_rejected_without_touching_target(repo, tmp_path):
    repo.commit({"m.py": "def f(): pass\n"})
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CacheUnavailable, match="symlink"):
        Cache(repo.root, link)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    "source",
    [
        "x=b'bytes'\ny=2j\nz=...\n",
        "def f(x: int=1):\n    return x\n",
        "class C:\n    @staticmethod\n    def f(): return 1\n",
    ],
)
def test_json_ast_roundtrip_keeps_exact_structure(source):
    parsed = ast.parse(source)
    result = read_ast(json.dumps(ast_data(parsed)), 100000)
    assert ast.dump(result, include_attributes=True) == ast.dump(parsed, include_attributes=True)


def test_cached_partial_snapshot_is_distinct_from_corruption(repo):
    repo.commit({"m.py": "def ???"})
    cache = Cache(repo.root)
    expected, _ = index(repo, cache)
    stats = IndexStats()
    result, _ = index(repo, cache, stats=stats)
    assert result == expected and result.diagnostics
    assert stats.snapshot_hits == 1 and cache.doctor()["healthy"]


def test_cache_contains_no_target_execution(repo):
    source = "from pathlib import Path\nPath('EXECUTED').write_text('bad')\ndef f(): return 1\n"
    repo.commit({"m.py": source})
    cache = Cache(repo.root)
    index(repo, cache)
    index(repo, cache)
    repo.commit({"n.py": "from m import f\ndef g(): return f()\n"})
    assert index(repo, cache)[0] == index(repo, None)[0]
    assert not (repo.root / "EXECUTED").exists()


def test_reader_sees_previous_complete_snapshot_during_staging(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    cache = Cache(repo.root)
    prior, config = index(repo, cache)
    key = key_for(cache, prior.tree, config)
    with cache.connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE snapshots SET status='staging' WHERE key=?", (key,))
        assert cache.get_snapshot(key, prior.tree, config.fingerprint) == prior
        writer.rollback()


def test_cache_source_cannot_be_substituted_for_a_git_blob(repo):
    from ripple.cache_codec import checksum

    repo.commit({"m.py": "def f(): return 1\n"})
    cache = Cache(repo.root)
    index(repo, cache)
    with cache.connection() as connection, connection:
        key, parsed = connection.execute("SELECT key,ast FROM syntax").fetchone()
        data = b"def f(): return 9\n"
        connection.execute(
            "UPDATE syntax SET source=?,checksum=? WHERE key=?",
            (data, checksum(data + parsed.encode()), key),
        )
    repo.commit({"new.py": "def g(): return 2\n"})
    stats = IndexStats()
    assert index(repo, cache, stats=stats)[0] == index(repo, None)[0]
    assert stats.syntax_hits == 0 and stats.warnings


def test_last_analysis_metadata_is_persisted_and_staleness_is_visible(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    repo.commit({"m.py": "def f(): return 2\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    cache = Cache(repo.root)
    last = cache.status()["last_analysis"]
    assert last["comparison"]["head_tree"] == report.comparison.head_tree
    status = json.loads(cli(repo, "status", "--format", "json").stdout)
    assert not status["last_analysis_stale"]
    repo.commit({"m.py": "def f(): return 3\n"})
    status = json.loads(cli(repo, "status", "--format", "json").stdout)
    assert status["last_analysis_stale"]
    assert cache.doctor()["healthy"]
    with cache.connection() as connection, connection:
        connection.execute("UPDATE analysis_runs SET checksum='bad'")
    assert not cache.doctor()["healthy"]
    cache.clear()
    assert cache.status()["last_analysis"] is None


def test_v2_migration_adds_analysis_metadata_without_discarding_snapshot(repo):
    repo.commit({"m.py": "def f(): pass\n"})
    cache = Cache(repo.root)
    prior, config = index(repo, cache)
    with cache.connection() as connection, connection:
        connection.execute("DROP TABLE analysis_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version=3")
        connection.execute("PRAGMA user_version=2")
    migrated = Cache(repo.root)
    assert migrated.repaired is None
    assert (
        migrated.get_snapshot(key_for(cache, prior.tree, config), prior.tree, config.fingerprint)
        == prior
    )


def test_scoped_clear_can_remove_orphan_graph_corruption(repo):
    repo.commit({"m.py": "def f(): pass\n"})
    cache = Cache(repo.root)
    index(repo, cache)
    with cache.connection() as connection, connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO edges VALUES('missing',0,'absent','absent','CALLS','{}')")
    assert not cache.doctor()["healthy"]
    cache.clear()
    assert cache.doctor()["healthy"]


def test_snapshot_rebuild_preserves_last_completed_analysis_until_replaced(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    repo.commit({"m.py": "def f(): return 2\n"})
    analyze(repo.root, "HEAD~1", "HEAD", Policy())
    cache = Cache(repo.root)
    prior = cache.status()["last_analysis"]
    index(repo, cache, force=True)
    assert cache.status()["last_analysis"] == prior
    assert cache.doctor()["healthy"]
