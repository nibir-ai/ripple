"""Small reproducible local cache measurement; creates only a temporary fixture."""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from ripple.cache import Cache
from ripple.git_source import GitSource
from ripple.indexing import IndexStats, index_snapshot


def measure() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ripple-cache-benchmark-") as temporary:
        base = Path(temporary)
        root = base / "repo"
        root.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Ripple benchmark",
            GIT_AUTHOR_EMAIL="bench@example.invalid",
            GIT_COMMITTER_NAME="Ripple benchmark",
            GIT_COMMITTER_EMAIL="bench@example.invalid",
        )

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false", *args],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                timeout=20,
            )

        git("init", "--quiet")
        for i in range(64):
            code = "def f0(x): return x + 1\n"
            code += "".join(f"def f{j}(x): return f0(x)\n" for j in range(1, 16))
            (root / f"m{i}.py").write_bytes(code.encode())
        git("add", "--all")
        git("commit", "--quiet", "-m", "fixture")
        source = GitSource(root)
        tree = source.resolve("HEAD")
        cache = Cache(root, base / "cache")

        def run(cached: bool, revision: str = tree) -> tuple[object, float, IndexStats]:
            stats = IndexStats()
            start = time.perf_counter()
            snapshot, _ = index_snapshot(
                source, revision, cache=cache if cached else None, stats=stats
            )
            return snapshot, time.perf_counter() - start, stats

        cold, cold_time, cold_stats = run(True)
        warm = [run(True) for _ in range(3)]
        full = [run(False) for _ in range(3)]
        assert all(result[0] == cold for result in [*warm, *full])
        changed = root / "m0.py"
        changed.write_bytes(changed.read_bytes().replace(b"x + 1", b"x + 2"))
        git("add", "--all")
        git("commit", "--quiet", "-m", "one-file edit")
        changed_tree = source.resolve("HEAD")
        incremental, incremental_time, incremental_stats = run(True, changed_tree)
        rebuilt, rebuild_time, rebuild_stats = run(False, changed_tree)
        assert incremental == rebuilt
        return {
            "platform": platform.system(),
            "python": platform.python_version(),
            "fixture_files": 64,
            "functions_per_file": 16,
            "warm_repetitions": 3,
            "cold_cache_seconds": cold_time,
            "cold_work": asdict(cold_stats),
            "warm_median_seconds": statistics.median(r[1] for r in warm),
            "warm_work": asdict(warm[-1][2]),
            "full_median_seconds": statistics.median(r[1] for r in full),
            "incremental_seconds": incremental_time,
            "incremental_work": asdict(incremental_stats),
            "changed_full_seconds": rebuild_time,
            "changed_full_work": asdict(rebuild_stats),
            "snapshot_parity": True,
            "scope": "Local synthetic fixture; not a production throughput guarantee.",
        }


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2, sort_keys=True))
