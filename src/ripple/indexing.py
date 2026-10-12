"""Snapshot orchestration: exact graph reuse or content syntax reuse/full resolution."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from ripple import frontend, git_source
from ripple.cache import Cache, CacheUnavailable, snapshot_key
from ripple.config import SourceConfig, load_config
from ripple.git_source import GitSource, SourceFile
from ripple.models import Snapshot, stable_id


@dataclass
class IndexStats:
    snapshot_hits: int = 0
    syntax_hits: int = 0
    parsed_files: int = 0
    blob_reads: int = 0
    resolution_passes: int = 0
    warnings: list[str] = field(default_factory=list)


def limits_key() -> str:
    return stable_id(
        str(frontend.MAX_AST_NODES),
        str(git_source.MAX_FILE_BYTES),
        str(git_source.MAX_TOTAL_BYTES),
        str(git_source.MAX_FILES),
        *sorted(git_source.EXCLUDED_PARTS),
    )


def open_cache(source: GitSource, base: Path | None, stats: IndexStats) -> Cache | None:
    try:
        cache = Cache(source.root, base)
        if cache.repaired:
            stats.warnings.append(cache.repaired)
        return cache
    except (CacheUnavailable, OSError, ValueError) as exc:
        stats.warnings.append(str(exc))
        return None


def key_for(cache: Cache, tree: str, config: SourceConfig) -> str:
    return snapshot_key(
        cache.identity,
        tree,
        config.fingerprint,
        stable_id(limits_key(), json.dumps(config.source_roots)),
    )


def index_snapshot(
    source: GitSource,
    tree: str,
    roots: tuple[str, ...] | None = None,
    cache: Cache | None = None,
    *,
    force: bool = False,
    stats: IndexStats | None = None,
) -> tuple[Snapshot, SourceConfig]:
    stats = stats if stats is not None else IndexStats()
    entries, excluded = source.inventory(tree)
    config = load_config(source, tree, [SourceFile(e.path, b"") for e in entries], roots, excluded)
    # Preserve reporting order as well as semantic configuration compatibility.
    key = key_for(cache, tree, config) if cache else ""
    if cache is not None and not force:
        try:
            snapshot = cache.get_snapshot(key, tree, config.fingerprint)
            if snapshot is not None:
                stats.snapshot_hits += 1
                return snapshot, config
        except CacheUnavailable as exc:
            stats.warnings.append(str(exc))
    syntax: dict[str, ast.Module] = {}
    files: list[SourceFile] = []
    reused: set[str] = set()
    for entry in entries:
        if config.module_for(entry.path) is None:
            files.append(SourceFile(entry.path, b""))
            continue
        if cache is not None and not force:
            try:
                hit = cache.get_syntax(entry.blob, frontend.MAX_AST_NODES)
                if hit is not None:
                    contents, parsed = hit
                    if len(contents) != entry.size:
                        raise CacheUnavailable("Cached source length mismatch; reparse required.")
                    syntax[entry.path] = parsed
                    files.append(SourceFile(entry.path, contents))
                    reused.add(entry.path)
                    stats.syntax_hits += 1
                    continue
            except CacheUnavailable as exc:
                stats.warnings.append(str(exc))
        files.append(source.read_entry(entry))
        stats.blob_reads += 1
        stats.parsed_files += 1
    stats.resolution_passes += 1
    try:
        snapshot = frontend.build_snapshot(tree, files, excluded, config, syntax)
        if any(d.code == "parse_error" and d.path in reused for d in snapshot.diagnostics):
            raise ValueError("Cached syntax extraction failed")
    except (TypeError, AttributeError, KeyError, ValueError, RecursionError) as exc:
        if not reused:
            raise
        stats.warnings.append(f"Cached syntax rejected ({type(exc).__name__}); rebuilding fully.")
        files = [source.read_entry(e) for e in entries]
        stats.blob_reads += len(files)
        stats.parsed_files += len(files)
        syntax = {}
        reused.clear()
        snapshot = frontend.build_snapshot(tree, files, excluded, config, syntax)
    if cache is not None:
        contents_by_path = {f.path: f.contents for f in files}
        facts = [
            (e.blob, contents_by_path[e.path], syntax[e.path])
            for e in entries
            if e.path in syntax and e.path not in reused
        ]
        try:
            cache.publish(key, snapshot, facts, frontend.MAX_AST_NODES)
        except (CacheUnavailable, OSError, ValueError, RecursionError) as exc:
            stats.warnings.append(str(exc))
    return snapshot, config
