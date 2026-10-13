"""Load immutable explorer sessions and bounded source excerpts without UI dependencies."""

from __future__ import annotations

import io
import re
import threading
import tokenize
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ripple import __version__
from ripple.analysis import analyze
from ripple.cache import Cache, CacheUnavailable
from ripple.config import load_config
from ripple.errors import RippleError
from ripple.git_source import GitSource, SourceEntry, SourceFile
from ripple.models import Location, Policy, Report
from ripple.reports import read_report

MAX_SNIPPET_BYTES = 8192


@dataclass(frozen=True)
class SourceExcerpt:
    tree: str
    site: Location
    first_line: int
    lines: tuple[str, ...]
    truncated: bool
    message: str = ""


class SnapshotReader:
    def __init__(self, source: GitSource):
        self.source = source
        self.entries: dict[str, dict[str, SourceEntry]] = {}
        self.blobs: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self.lock = threading.Lock()

    def excerpt(
        self,
        tree: str,
        site: Location,
        cancelled: Callable[[], bool] | None = None,
    ) -> SourceExcerpt:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
            raise ValueError("Source inspection requires an immutable tree ID")
        with self.lock:
            if cancelled is not None and cancelled():
                return SourceExcerpt(tree, site, 0, (), False, "Preview cancelled.")
            if tree not in self.entries:
                entries, _ = self.source.inventory(tree)
                self.entries[tree] = {e.path: e for e in entries}
            entry = self.entries[tree].get(site.path)
            if entry is None:
                return SourceExcerpt(
                    tree, site, 0, (), False, "Source is absent or excluded in this immutable tree."
                )
            key = (tree, site.path)
            if key not in self.blobs:
                self.blobs[key] = self.source.read_entry(entry).contents
                if len(self.blobs) > 8:
                    self.blobs.popitem(last=False)
            data = self.blobs[key]
            self.blobs.move_to_end(key)
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        lines = data.decode(encoding).splitlines()
        if site.line < 1 or site.line > len(lines):
            return SourceExcerpt(tree, site, 0, (), False, "Report location is outside source.")
        first = max(1, site.line - 3)
        last = min(len(lines), site.line + 4)
        chosen: list[str] = []
        truncated = False
        for number, line in enumerate(lines[first - 1 : last], first):
            encoded = line.encode("utf-8", errors="replace")
            if len(encoded) > 1000:
                start = max(0, site.column - 160) if number == site.line else 0
                window = encoded[start : start + 1000].decode("utf-8", errors="ignore")
                chosen.append(("… " if start else "") + window + " …")
                truncated = True
            else:
                chosen.append(line)
        return SourceExcerpt(tree, site, first, tuple(chosen), truncated)


@dataclass(frozen=True)
class ExplorerSession:
    report: Report
    reader: SnapshotReader | None
    label: str
    stale: bool = False


def report_is_stale(report: Report, source: GitSource) -> bool:
    tree = source.resolve("HEAD")
    entries, excluded = source.inventory(tree)
    config = load_config(
        source, tree, [SourceFile(e.path, b"") for e in entries], excluded=excluded
    )
    return (
        tree != report.comparison.head_tree
        or config.fingerprint != report.tool.get("head_config")
        or report.tool.get("version") != __version__
    )


def load_session(
    repo: Path,
    report_path: Path | None = None,
    analysis_id: str | None = None,
    base: str | None = None,
    head: str | None = None,
    cache_dir: Path | None = None,
    allow_stale: bool = False,
    refresh: bool = False,
) -> ExplorerSession:
    if report_path is not None:
        report = read_report(report_path)
        try:
            reader = SnapshotReader(GitSource(repo))
        except RippleError:
            reader = None
        return ExplorerSession(report, reader, "EXPORTED REPORT · original revision evidence")
    source = GitSource(repo)
    if analysis_id is not None:
        try:
            report = Cache(source.root, cache_dir, repair=False).load_report(analysis_id)
        except (CacheUnavailable, ValueError, TypeError, KeyError) as exc:
            raise RippleError(
                "RPL-EXPLORE-006", "Saved analysis is unavailable; run analyze or use --report.", 2
            ) from exc
        stale = report_is_stale(report, source)
        if stale and not allow_stale:
            raise RippleError(
                "RPL-EXPLORE-003", "Saved analysis is stale; use --allow-stale or run analyze.", 2
            )
        return ExplorerSession(report, SnapshotReader(source), "SAVED ANALYSIS", stale)
    if base is None and head is None and not refresh:
        try:
            report = Cache(source.root, cache_dir, repair=False).load_report()
            if not report_is_stale(report, source):
                return ExplorerSession(report, SnapshotReader(source), "CURRENT SAVED ANALYSIS")
        except (CacheUnavailable, ValueError, TypeError, KeyError):
            pass
    report = analyze(repo, base or "HEAD~1", head or "HEAD", Policy(), cache_dir=cache_dir)
    return ExplorerSession(report, SnapshotReader(source), "ANALYZED IMMUTABLE TREES")
