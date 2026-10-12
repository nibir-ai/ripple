"""Conservative cross-revision relations; structural resemblance is only a candidate."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ripple.models import (
    Change,
    Diagnostic,
    FileChange,
    MatchCandidate,
    Snapshot,
    Symbol,
    SymbolMatch,
    stable_id,
)

CALLABLE_KINDS = {"function", "async_function", "method", "async_method"}
UNKNOWN_CODES = {
    "parse_error",
    "ast_node_limit",
    "file_size_limit",
    "aggregate_size_limit",
    "file_count_limit",
    "symlink_excluded",
    "default_exclusion",
    "source_root_exclusion",
}


@dataclass(frozen=True)
class MatchResult:
    changes: tuple[Change, ...]
    matches: tuple[SymbolMatch, ...]
    candidates: tuple[MatchCandidate, ...]
    diagnostics: tuple[Diagnostic, ...]


def compare(
    before: Snapshot,
    after: Snapshot,
    files: tuple[FileChange, ...] = (),
) -> MatchResult:
    old_symbols = {s.id: s for s in before.symbols}
    new_symbols = {s.id: s for s in after.symbols}
    old: dict[tuple[str, str, str], list[Symbol]] = defaultdict(list)
    new: dict[tuple[str, str, str], list[Symbol]] = defaultdict(list)
    unknown = {
        d.path
        for snapshot in (before, after)
        for d in snapshot.diagnostics
        if d.code in UNKNOWN_CODES
    }
    # A failed renamed destination is not evidence that all old definitions were removed.
    for file in files:
        if file.status == "renamed" and (file.before_path in unknown or file.after_path in unknown):
            unknown.update(p for p in (file.before_path, file.after_path) if p is not None)
    for snapshot_symbols, index in ((before.symbols, old), (after.symbols, new)):
        for symbol in snapshot_symbols:
            if symbol.location.path not in unknown:
                index[(symbol.location.path, symbol.qualname, symbol.kind)].append(symbol)
    matches: list[SymbolMatch] = []
    diagnostics: list[Diagnostic] = []
    matched_old: set[str] = set()
    matched_new: set[str] = set()
    unchanged_duplicates: set[str] = set()

    def pair(a: Symbol, b: Symbol, rule: str) -> None:
        matches.append(SymbolMatch(a.id, b.id, rule))
        matched_old.add(a.id)
        matched_new.add(b.id)

    for key in sorted(old.keys() | new.keys()):
        olds, news = old[key], new[key]
        if len(olds) == len(news) == 1:
            pair(olds[0], news[0], "unique_path_qualname_kind-v1")
        elif before.tree != after.tree and (len(olds) > 1 or len(news) > 1):

            def ordered_facts(symbols: list[Symbol]) -> list[tuple[str, ...]]:
                return [
                    (s.module, s.body_hash, s.signature_hash, s.decorators_hash, s.docstring_hash)
                    for s in sorted(symbols, key=lambda s: (s.location.line, s.location.column))
                ]

            if ordered_facts(olds) == ordered_facts(news):
                # Equal ordered definitions prove no semantic fact changed, without
                # claiming that duplicate sites have a cross-revision identity.
                unchanged_duplicates.update(s.id for s in [*olds, *news])
                continue
            diagnostics.append(
                Diagnostic(
                    "ambiguous_symbol_match",
                    "Duplicate definitions remain unmatched; no line-based pairing.",
                    key[0],
                )
            )

    renames = {f.before_path: f for f in files if f.status == "renamed"}
    for (path, qualname, kind), olds in sorted(old.items()):
        renamed_file = renames.get(path)
        if renamed_file is None or renamed_file.after_path is None:
            continue
        news = new.get((renamed_file.after_path, qualname, kind), [])
        if len(olds) == len(news) == 1:
            a, b = olds[0], news[0]
            if a.id not in matched_old and b.id not in matched_new:
                pair(a, b, f"git_file_rename_{renamed_file.similarity}_same_qualname_kind-v1")
        elif olds and news:
            diagnostics.append(
                Diagnostic(
                    "ambiguous_symbol_match",
                    "File rename does not disambiguate duplicate definitions.",
                    path,
                )
            )

    if before.tree == after.tree:
        return MatchResult((), tuple(sorted(matches, key=lambda m: m.before_symbol_id)), (), ())

    # Group by the full interface/body/decorator structure, rather than all-pairs search.
    # Candidates never merge identities or caller summaries. Even unique resemblance
    # cannot prove a rename: an independent replacement can have identical structure.
    structural_old: dict[tuple[str, str, str, str, str], list[Symbol]] = defaultdict(list)
    structural_new: dict[tuple[str, str, str, str, str], list[Symbol]] = defaultdict(list)
    for index, groups, matched in (
        (old, structural_old, matched_old),
        (new, structural_new, matched_new),
    ):
        for symbols in index.values():
            for symbol in symbols:
                if (
                    symbol.id not in matched
                    and symbol.id not in unchanged_duplicates
                    and symbol.kind in CALLABLE_KINDS
                ):
                    groups[
                        (
                            symbol.kind,
                            symbol.qualname.rpartition(".")[0],
                            symbol.body_hash,
                            symbol.signature_hash,
                            symbol.decorators_hash,
                        )
                    ].append(symbol)
    candidates = []
    for structure in sorted(structural_old.keys() & structural_new.keys()):
        olds = sorted(structural_old[structure], key=lambda s: (s.logical_id, s.id))
        news = sorted(structural_new[structure], key=lambda s: (s.logical_id, s.id))
        unique = len(olds) == len(news) == 1
        candidates.append(
            MatchCandidate(
                tuple(s.id for s in olds),
                tuple(s.id for s in news),
                tuple(s.display_name for s in olds),
                tuple(s.display_name for s in news),
                "unique_structural_candidate-v1" if unique else "ambiguous_structural_candidate-v1",
                "Same callable kind, parent qualname, normalized body, signature and decorators. "
                "Resemblance does not prove identity; "
                "additions/removals and graphs remain separate.",
            )
        )
        if not unique:
            diagnostics.append(
                Diagnostic(
                    "ambiguous_relocation",
                    "Several structurally identical definitions; no automatic relocation.",
                    olds[0].location.path,
                )
            )

    changes = []

    def change(kind: str, a: Symbol | None, b: Symbol | None, rule: str) -> None:
        selected = b or a
        assert selected is not None
        changes.append(
            Change(
                stable_id(kind, a.id if a else "", b.id if b else ""),
                kind,
                selected.display_name,
                a.id if a else None,
                b.id if b else None,
                a.location if a else None,
                b.location if b else None,
                rule,
                a.display_name if a else None,
                b.display_name if b else None,
            )
        )

    if before.tree != after.tree:
        for match in matches:
            a, b = old_symbols[match.before_symbol_id], new_symbols[match.after_symbol_id]
            if a.location.path != b.location.path or a.display_name != b.display_name:
                change("symbol_relocated", a, b, match.match_rule)
            if a.body_hash != b.body_hash:
                change(
                    "body_modified"
                    if b.kind in CALLABLE_KINDS
                    else "class_body_modified"
                    if b.kind == "class"
                    else "module_body_modified",
                    a,
                    b,
                    match.match_rule,
                )
            if a.signature_hash != b.signature_hash:
                change(
                    "bases_modified"
                    if b.kind == "class"
                    else "imports_modified"
                    if b.kind == "module"
                    else "signature_modified",
                    a,
                    b,
                    match.match_rule,
                )
            if a.decorators_hash != b.decorators_hash:
                change("decorators_modified", a, b, match.match_rule)
            if a.docstring_hash != b.docstring_hash:
                change("docstring_modified", a, b, match.match_rule)
        for index, matched, side in ((old, matched_old, "before"), (new, matched_new, "after")):
            for symbols in index.values():
                for symbol in symbols:
                    if symbol.id not in matched and symbol.id not in unchanged_duplicates:
                        change(
                            "symbol_removed" if side == "before" else "symbol_added",
                            symbol if side == "before" else None,
                            symbol if side == "after" else None,
                            "unmatched-v1",
                        )
    return MatchResult(
        tuple(sorted(changes, key=lambda c: (c.display_name, c.kind, c.change_id))),
        tuple(sorted(matches, key=lambda m: (m.before_symbol_id, m.after_symbol_id))),
        tuple(candidates),
        tuple(diagnostics),
    )
