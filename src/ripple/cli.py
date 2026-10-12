from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ripple import __version__
from ripple.analysis import analyze
from ripple.cache import Cache, CacheUnavailable, default_cache_dir, workspace_id
from ripple.config import load_config
from ripple.errors import RippleError
from ripple.git_source import GitSource, SourceFile
from ripple.indexing import IndexStats, index_snapshot, key_for, open_cache
from ripple.models import Policy
from ripple.output import render_text, safe_text


def positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def rename_threshold(value: str) -> int:
    number = positive(value)
    if not 50 <= number <= 100:
        raise argparse.ArgumentTypeError("must be 50 through 100")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="ripple",
        description="Offline Python static change-impact analysis. Reads immutable Git trees; "
        "uncommitted changes are excluded. Development preview.",
    )
    result.add_argument("--version", action="version", version=f"ripple {__version__}")
    commands = result.add_subparsers(dest="command", required=True)
    index = commands.add_parser(
        "index", help="Index an immutable Git tree with safe snapshot caching"
    )
    index.add_argument(
        "--ref", default="HEAD", help="Immutable revision to inspect (default: HEAD)"
    )
    comparison = commands.add_parser(
        "analyze", help="Compare functions and trace potential callers"
    )
    comparison.add_argument("--base", default="HEAD~1", help="Base ref (default: HEAD~1)")
    comparison.add_argument("--head", default="HEAD", help="Head ref (default: HEAD)")
    comparison.add_argument("--max-depth", type=positive, default=8)
    comparison.add_argument("--max-nodes", type=positive, default=5000)
    comparison.add_argument(
        "--rename-threshold",
        type=rename_threshold,
        default=90,
        help="Git file rename similarity, 50–100 percent (default: 90); heuristic evidence",
    )
    status = commands.add_parser("status", help="Inspect this workspace's cache and staleness")
    doctor = commands.add_parser("doctor", help="Check Git, configuration and cache integrity")
    cache_command = commands.add_parser("cache", help="Manage only this workspace's cached facts")
    clear = cache_command.add_subparsers(dest="cache_command", required=True).add_parser(
        "clear", help="Clear rows in this workspace's owned cache; preserve source files"
    )
    for command in (index, comparison):
        command.add_argument(
            "--no-cache", action="store_true", help="Full analysis without cache reads/writes"
        )
        command.add_argument(
            "--force",
            action="store_true",
            help="Rebuild parsing/resolution before atomically replacing cached state",
        )
        command.add_argument(
            "--cache-stats",
            action="store_true",
            help="Print measured cache work counters to stderr",
        )
        command.add_argument(
            "--repo", type=Path, default=Path.cwd(), help="Git working tree directory"
        )
        command.add_argument("--format", choices=("text", "json"), default="text")
        command.add_argument("--strict", action="store_true", help="Exit 4 for incomplete analysis")
        command.add_argument(
            "--source-root",
            action="append",
            help="Repository-relative import "
            "root; repeat for multiple roots. Overrides snapshot TOML.",
        )
    for command in (status, doctor, clear):
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--format", choices=("text", "json"), default="text")
    for command in (index, comparison, status, doctor, clear):
        command.add_argument(
            "--cache-dir",
            type=Path,
            help="Cache base directory; workspace namespace is added automatically",
        )
    return result


def maintenance(args: argparse.Namespace) -> int:
    source = GitSource(args.repo)
    base = args.cache_dir or default_cache_dir()
    path = base.absolute() / workspace_id(source.root) / "index.sqlite3"
    data: dict[str, object] = {"cache_path": str(path), "cache_exists": path.exists()}
    if args.command != "cache":
        tree = source.resolve("HEAD")
        entries, excluded = source.inventory(tree)
        config = load_config(
            source, tree, [SourceFile(e.path, b"") for e in entries], excluded=excluded
        )
        data.update(head_tree=tree, source_roots=list(config.source_roots), stale=True)
    try:
        cache = Cache(source.root, args.cache_dir, repair=False)
        if args.command == "cache":
            cache.clear()
            data.update(cleared=True, scope="this workspace's snapshot and syntax rows")
        else:
            data.update(cache.status())
            rows = data["snapshots"]
            assert isinstance(rows, list)
            data["head_tree"] = tree
            data["stale"] = not any(
                r["key"] == key_for(cache, tree, config) and r["compatible"] for r in rows
            )
            last = data.get("last_analysis")
            data["last_analysis_stale"] = not isinstance(last, dict) or last["head_key"] != key_for(
                cache, tree, config
            )
            if args.command == "doctor":
                data.update(cache.doctor())
    except (CacheUnavailable, OSError, ValueError, KeyError) as exc:
        data.update(healthy=False, advice=str(exc))
        if isinstance(exc, CacheUnavailable) and str(exc).startswith("Cache does not exist"):
            data.update(healthy=True, advice="No cached snapshots; run ripple index.")
            if args.command == "cache":
                data.update(cleared=True)
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2))
    else:
        for key, value in data.items():
            print(safe_text(f"{key}: {value}"))
    return 4 if args.command in {"doctor", "cache"} and data.get("healthy") is False else 0


def cache_messages(stats: IndexStats, verbose: bool) -> None:
    for warning in sorted(set(stats.warnings)):
        print(safe_text(f"RPL-CACHE-001: {warning}"), file=sys.stderr)
    if verbose:
        print(
            json.dumps({"cache_stats": asdict(stats)}, ensure_ascii=True, sort_keys=True),
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"status", "doctor", "cache"}:
            return maintenance(args)
        stats = IndexStats()
        if args.command == "analyze":
            report = analyze(
                args.repo,
                args.base,
                args.head,
                Policy(max_depth=args.max_depth, max_nodes=args.max_nodes),
                tuple(args.source_root) if args.source_root is not None else None,
                args.rename_threshold,
                args.cache_dir,
                not args.no_cache,
                args.force,
                stats,
            )
            cache_messages(stats, args.cache_stats)
            sys.stdout.write(report.to_json() if args.format == "json" else render_text(report))
            return 4 if args.strict and report.completeness.status == "partial" else 0
        source = GitSource(args.repo)
        tree = source.resolve(args.ref)
        cache = open_cache(source, args.cache_dir, stats) if not args.no_cache else None
        snapshot, config = index_snapshot(
            source,
            tree,
            tuple(args.source_root) if args.source_root is not None else None,
            cache,
            force=args.force,
            stats=stats,
        )
        cache_messages(stats, args.cache_stats)
        if args.format == "json":
            print(
                json.dumps(
                    {"schema": "ripple.index.v1", **asdict(snapshot)},
                    ensure_ascii=True,
                    sort_keys=True,
                    indent=2,
                )
            )
        else:
            print(f"Indexed {snapshot.parsed_files} files in immutable tree {tree}.")
            print(
                safe_text(
                    f"Source roots: {', '.join(snapshot.source_roots)} ({config.provenance})."
                )
            )
            print(
                f"{len(snapshot.symbols)} symbols; {len(snapshot.references)} reference sites; "
                f"{len(snapshot.diagnostics)} diagnostics."
            )
            for diagnostic in snapshot.diagnostics:
                print(
                    safe_text(
                        f"{diagnostic.code}: {diagnostic.path}:{diagnostic.line} "
                        f"- {diagnostic.message}"
                    )
                )
        return 4 if args.strict and snapshot.diagnostics else 0
    except RippleError as exc:
        print(safe_text(f"{exc.code}: {exc}"), file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError) as exc:
        print(safe_text(f"RPL-IO-001: Operation failed ({type(exc).__name__})."), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
