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
from ripple.explain import explanation, select_impact
from ripple.git_source import GitSource, SourceFile
from ripple.indexing import IndexStats, index_snapshot, key_for, open_cache
from ripple.models import Policy
from ripple.output import render_text, safe_text
from ripple.reports import read_report


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


def query_timeout(value: str) -> int:
    number = positive(value)
    if number > 3600000:
        raise argparse.ArgumentTypeError("must be 1 through 3600000 milliseconds")
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
    comparison.add_argument("--max-edges", type=positive, default=100000)
    comparison.add_argument("--max-paths-per-target", type=positive, default=16)
    comparison.add_argument("--query-timeout-ms", type=query_timeout, default=10000)
    comparison.add_argument(
        "--no-candidates", action="store_true", help="Use resolved call edges only (the default)"
    )
    comparison.add_argument(
        "--rename-threshold",
        type=rename_threshold,
        default=90,
        help="Git file rename similarity, 50–100 percent (default: 90); heuristic evidence",
    )
    status = commands.add_parser("status", help="Inspect this workspace's cache and staleness")
    doctor = commands.add_parser("doctor", help="Check Git, configuration and cache integrity")
    explain = commands.add_parser("explain", help="Inspect evidence for one impacted symbol")
    explain.add_argument("name", nargs="?")
    selector = explain.add_mutually_exclusive_group()
    selector.add_argument("--symbol-id")
    selector.add_argument("--path", help="Repository-relative path:line")
    saved = explain.add_mutually_exclusive_group()
    saved.add_argument("--report", type=Path, help="Read a saved JSON report")
    saved.add_argument("--analysis", help="Exact saved analysis ID")
    explain.add_argument(
        "--allow-stale",
        action="store_true",
        help="Inspect a historical cached report with its original provenance",
    )
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
    for command in (status, doctor, clear, explain):
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--format", choices=("text", "json"), default="text")
    for command in (index, comparison, status, doctor, clear, explain):
        command.add_argument(
            "--cache-dir",
            type=Path,
            help="Cache base directory; workspace namespace is added automatically",
        )
    return result


def explain_command(args: argparse.Namespace) -> int:
    if args.name is not None and (args.symbol_id is not None or args.path is not None):
        raise RippleError("RPL-EXPLAIN-001", "Use exactly one symbol selector.", 2)
    try:
        if args.report is not None:
            report = read_report(args.report)
        else:
            source = GitSource(args.repo)
            report = Cache(source.root, args.cache_dir, repair=False).load_report(args.analysis)
            tree = source.resolve("HEAD")
            entries, excluded = source.inventory(tree)
            config = load_config(
                source, tree, [SourceFile(e.path, b"") for e in entries], excluded=excluded
            )
            stale = (
                tree != report.comparison.head_tree
                or config.fingerprint != report.tool.get("head_config")
                or report.tool.get("version") != __version__
            )
            if stale and not args.allow_stale:
                raise RippleError(
                    "RPL-EXPLAIN-004",
                    "Saved analysis is stale; rerun analyze or use --allow-stale.",
                    2,
                )
    except (CacheUnavailable, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise RippleError(
            "RPL-REPORT-001", "Invalid or unavailable report; rerun analyze.", 2
        ) from exc
    impact = select_impact(report, args.name, args.symbol_id, args.path)
    selected = explanation(report, impact)
    sys.stdout.write(selected.to_json() if args.format == "json" else render_text(selected))
    return 0


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
        if args.command == "explain":
            return explain_command(args)
        if args.command in {"status", "doctor", "cache"}:
            return maintenance(args)
        stats = IndexStats()
        if args.command == "analyze":
            report = analyze(
                args.repo,
                args.base,
                args.head,
                Policy(
                    max_depth=args.max_depth,
                    max_nodes=args.max_nodes,
                    max_edges=args.max_edges,
                    max_paths_per_target=args.max_paths_per_target,
                    query_timeout_ms=args.query_timeout_ms,
                ),
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
