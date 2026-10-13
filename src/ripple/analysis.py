"""Pure matching and bounded reverse traversal of snapshot call graphs."""

from __future__ import annotations

import sys
import time
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path

from ripple import __version__
from ripple.cache import CacheUnavailable
from ripple.git_source import GitSource
from ripple.indexing import IndexStats, index_snapshot, key_for, open_cache
from ripple.matching import CALLABLE_KINDS, compare
from ripple.models import (
    Change,
    Comparison,
    Completeness,
    Edge,
    EvidencePath,
    Impact,
    ImpactGroup,
    Policy,
    Report,
    Snapshot,
    Step,
    Symbol,
    SymbolMatch,
    stable_id,
)
from ripple.reports import validate_report
from ripple.test_selection import related_tests_for


def changes_between(before: Snapshot, after: Snapshot) -> tuple[Change, ...]:
    """Compatibility wrapper for pure comparisons without Git rename evidence."""
    return compare(before, after).changes


def impacts_for(
    changes: tuple[Change, ...],
    before: Snapshot,
    after: Snapshot,
    policy: Policy,
    matches: tuple[SymbolMatch, ...] | None = None,
) -> tuple[tuple[Impact, ...], tuple[str, ...]]:
    collected: dict[str, tuple[Symbol, list[EvidencePath]]] = {}
    reasons: set[str] = set()
    relations = compare(before, after).matches if matches is None else matches
    canonical: dict[str, str] = {}
    for relation in relations:
        key = stable_id("match", relation.before_symbol_id, relation.after_symbol_id)
        canonical[relation.before_symbol_id] = key
        canonical[relation.after_symbol_id] = key
    locations: dict[str, dict[str, Symbol]] = defaultdict(dict)
    additional: set[str] = set()
    deadline = time.monotonic() + policy.query_timeout_ms / 1000
    work = 0

    def exhausted(*, edge: bool = False) -> bool:
        nonlocal work
        if time.monotonic() >= deadline:
            reasons.add("query_timeout")
        if edge:
            work += 1
            if work > policy.max_edges:
                reasons.add("max_edges")
        return bool(reasons & {"query_timeout", "max_edges"})

    for snapshot, side in [(before, "before"), (after, "after")]:
        if exhausted():
            break
        symbols = {s.id: s for s in snapshot.symbols}
        incoming: dict[str, list[Edge]] = defaultdict(list)
        for edge in snapshot.edges:
            if exhausted(edge=True):
                break
            if edge.edge_type == "CALLS":
                incoming[edge.target_id].append(edge)
        for neighbors in incoming.values():
            if exhausted():
                break
            neighbors.sort(
                key=lambda e: (
                    symbols[e.source_id].logical_id,
                    e.site.path,
                    e.site.line,
                    e.site.column,
                    e.resolver_rule,
                )
            )
        for change in changes:
            if exhausted():
                break
            seed = change.before_symbol_id if side == "before" else change.after_symbol_id
            if seed is None or seed not in symbols:
                continue
            # Non-callable facts and docstrings are visible changes, not caller-impact seeds.
            if symbols[seed].kind not in CALLABLE_KINDS or change.kind == "docstring_modified":
                continue
            queue: deque[tuple[str, tuple[Step, ...]]] = deque([(seed, ())])
            visited = {seed}
            while queue:
                if exhausted():
                    break
                target_id, path = queue.popleft()
                neighbors = incoming[target_id]
                if len(path) >= policy.max_depth:
                    if any(e.source_id not in visited for e in neighbors):
                        reasons.add("max_depth")
                    continue
                for edge in neighbors:
                    if exhausted(edge=True):
                        break
                    caller = symbols[edge.source_id]
                    if caller.id in visited:
                        if caller.id != seed:
                            additional.add(canonical.get(caller.id, caller.id))
                        elif target_id != seed:
                            additional.add(canonical.get(target_id, target_id))
                        continue
                    key = canonical.get(caller.id, caller.id)
                    if key not in collected and len(collected) >= policy.max_nodes:
                        reasons.add("max_nodes")
                        # Already included callers may still get other origins/provenance.
                        continue
                    visited.add(caller.id)
                    step = Step(
                        "CALLS",
                        caller.display_name,
                        symbols[target_id].display_name,
                        edge.site,
                        edge.resolver_rule,
                        edge.explanation,
                    )
                    next_path = (step, *path)
                    evidence = EvidencePath(change.change_id, side, snapshot.tree, next_path)  # type: ignore[arg-type]
                    if key not in collected:
                        collected[key] = (caller, [])
                    selected, paths = collected[key]
                    if len(paths) < policy.max_paths_per_target:
                        paths.append(evidence)
                    else:
                        reasons.add("max_paths_per_target")
                        additional.add(key)
                    locations[key][side] = caller
                    if side == "after":
                        collected[key] = (caller, paths)
                    queue.append((caller.id, next_path))
    impacts = []
    for key, (symbol, paths) in collected.items():
        paths.sort(
            key=lambda p: (
                p.origin_change_id,
                p.snapshot,
                tuple((s.site.path, s.site.line, s.site.column) for s in p.steps),
            )
        )
        depth = min(len(p.steps) for p in paths)
        sides = {p.snapshot for p in paths}
        provenance = "both" if len(sides) == 2 else next(iter(sides)) + "_only"
        impacts.append(
            Impact(
                symbol.id,
                symbol.display_name,
                "direct_caller" if depth == 1 else "transitive_caller",
                depth,
                provenance,
                "resolved",
                tuple(sorted({p.origin_change_id for p in paths})),
                tuple(paths),
                locations[key]["before"].location if "before" in locations[key] else None,
                locations[key]["after"].location if "after" in locations[key] else None,
                locations[key]["before"].display_name if "before" in locations[key] else None,
                locations[key]["after"].display_name if "after" in locations[key] else None,
                key in additional,
            )
        )
    return tuple(sorted(impacts, key=lambda i: (i.depth, i.display_name, i.symbol_id))), tuple(
        sorted(reasons)
    )


def analyze(
    directory: Path,
    base: str,
    head: str,
    policy: Policy,
    source_roots: tuple[str, ...] | None = None,
    rename_threshold: int = 90,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    force: bool = False,
    stats: IndexStats | None = None,
) -> Report:
    source = GitSource(directory)
    stats = stats if stats is not None else IndexStats()
    cache = open_cache(source, cache_dir, stats) if use_cache else None
    base_tree, head_tree = source.resolve(base), source.resolve(head)
    before, base_config = index_snapshot(
        source, base_tree, source_roots, cache, force=force, stats=stats
    )
    if head_tree == base_tree:
        after = before
        head_config = base_config
    else:
        after, head_config = index_snapshot(
            source, head_tree, source_roots, cache, force=force, stats=stats
        )
    file_changes = source.diff(base_tree, head_tree, rename_threshold)
    result = compare(before, after, file_changes)
    changes = result.changes
    impacts, limits = impacts_for(changes, before, after, policy, result.matches)
    related_tests = related_tests_for(changes, impacts, before, after, result.matches)
    groups = tuple(
        ImpactGroup(category, count, 20)
        for category in ("direct_caller", "transitive_caller")
        if (count := sum(i.category == category for i in impacts)) > 20
    )
    diagnostics: list[dict[str, str | int]] = []
    for snapshot, side in [(before, "before"), (after, "after")]:
        for diagnostic in snapshot.diagnostics:
            diagnostics.append({**asdict(diagnostic), "snapshot": side, "tree": snapshot.tree})
    for diagnostic in result.diagnostics:
        diagnostics.append({**asdict(diagnostic), "snapshot": "comparison", "tree": head_tree})
    unresolved = sum(
        r.resolution_state == "unresolved" for s in (before, after) for r in s.references
    )
    skipped = before.skipped_files + after.skipped_files
    reasons = tuple(
        sorted(
            {d.code for s in (before, after) for d in s.diagnostics}
            | {d.code for d in result.diagnostics}
            | set(limits)
        )
    )
    completeness = Completeness(
        "partial" if reasons else "complete_within_static_policy",
        before.parsed_files + after.parsed_files,
        skipped,
        unresolved,
        0,
        bool(limits),
        reasons,
        (
            "Counts cover both snapshot sides, including identical trees.",
            "Comparison uses normalized definitions and retains both revision graphs.",
            "Git rename similarity is heuristic; "
            "structural relocation candidates do not merge identities.",
            "Static evidence does not guarantee runtime behavior or safety.",
        ),
    )
    report = Report(
        "ripple.report.v1",
        {
            "name": "ripple",
            "version": __version__,
            "parser": "python.ast",
            "analyzer": "python-static-v1",
            "rename_threshold": str(rename_threshold),
            "base_config": before.config_fingerprint,
            "head_config": after.config_fingerprint,
            "parser_python": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        {"root": "."},
        Comparison(base, base_tree, head, head_tree, before.source_roots, after.source_roots),
        policy,
        {
            "changed_symbol_count": len({c.after_symbol_id or c.before_symbol_id for c in changes}),
            "change_fact_count": len(changes),
            "impacted_direct_count": sum(i.depth == 1 for i in impacts),
            "impacted_transitive_count": sum(i.depth > 1 for i in impacts),
            "candidate_impact_count": 0,
            "relocation_candidate_count": len(result.candidates),
            "related_test_count": sum(
                t.evidence_class != "import_reference_only" for t in related_tests
            ),
            "related_test_import_module_count": sum(
                t.evidence_class == "import_reference_only" for t in related_tests
            ),
        },
        changes,
        impacts,
        tuple(diagnostics),
        completeness,
        file_changes,
        result.matches,
        result.candidates,
        related_tests,
        groups,
    )
    validate_report(report)
    if cache is not None:
        try:
            cache.save_report(
                report,
                key_for(cache, base_tree, base_config),
                key_for(cache, head_tree, head_config),
            )
        except (CacheUnavailable, OSError, ValueError) as exc:
            stats.warnings.append(str(exc))
    return report
