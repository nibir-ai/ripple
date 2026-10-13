"""Static test associations, separate from runtime coverage and test execution."""

from __future__ import annotations

from pathlib import PurePosixPath

from ripple.models import (
    Change,
    Impact,
    RelatedTest,
    Side,
    Snapshot,
    Symbol,
    SymbolMatch,
    TestImport,
)


def test_file(path: str) -> bool:
    file = PurePosixPath(path)
    return "tests" in file.parts[:-1] or (
        file.name.startswith("test_") or file.name.endswith("_test.py")
    )


def test_symbol(symbol: Symbol) -> bool:
    parts = symbol.qualname.split(".")
    return test_file(symbol.location.path) and (
        (
            symbol.kind in {"function", "async_function"}
            and len(parts) == 1
            and parts[0].startswith("test_")
        )
        or (
            symbol.kind in {"method", "async_method"}
            and len(parts) == 2
            and parts[0].startswith("Test")
            and parts[1].startswith("test_")
        )
    )


def related_tests_for(
    changes: tuple[Change, ...],
    impacts: tuple[Impact, ...],
    before: Snapshot,
    after: Snapshot,
    matches: tuple[SymbolMatch, ...] = (),
) -> tuple[RelatedTest, ...]:
    symbols = {s.id: s for snapshot in (before, after) for s in snapshot.symbols}
    tests = [
        RelatedTest(
            impact.symbol_id,
            impact.display_name,
            "direct_static_call" if impact.depth == 1 else "transitive_static_call",
            impact.provenance,
            impact.origin_change_ids,
            impact.evidence_paths,
            before=impact.before,
            after=impact.after,
        )
        for impact in impacts
        if test_symbol(symbols[impact.symbol_id])
    ]
    # An import associates a test module, never an individual test function.
    canonical = {m.before_symbol_id: m.after_symbol_id for m in matches}
    imports: dict[str, list[TestImport]] = {}
    owners: dict[str, dict[str, Symbol]] = {}
    snapshots: tuple[tuple[Snapshot, Side], ...] = ((before, "before"), (after, "after"))
    for snapshot, side in snapshots:
        local = {s.id: s for s in snapshot.symbols}
        targets: dict[str, list[str]] = {}
        for change in changes:
            target = change.before_symbol_id if side == "before" else change.after_symbol_id
            if target is not None:
                targets.setdefault(target, []).append(change.change_id)
        for reference in snapshot.references:
            if reference.syntax_kind != "import" or reference.resolution_state != "resolved":
                continue
            owner = local[reference.source_id]
            if owner.kind != "module" or not test_file(owner.location.path):
                continue
            origins = targets.get(reference.target_id or "", [])
            if origins:
                key = canonical.get(owner.id, owner.id)
                imports.setdefault(key, []).append(
                    TestImport(
                        side,
                        snapshot.tree,
                        reference.location,
                        reference.resolver_rule,
                        tuple(sorted(origins)),
                    )
                )
                owners.setdefault(key, {})[side] = owner
    for key, evidence in imports.items():
        sides = owners[key]
        owner = sides.get("after") or sides["before"]
        tests.append(
            RelatedTest(
                owner.id,
                owner.display_name,
                "import_reference_only",
                "both" if len(sides) == 2 else next(iter(sides)) + "_only",
                tuple(sorted({origin for e in evidence for origin in e.origin_change_ids})),
                (),
                tuple(evidence),
                sides["before"].location if "before" in sides else None,
                sides["after"].location if "after" in sides else None,
            )
        )
    return tuple(
        sorted(
            tests,
            key=lambda t: (
                {"direct_static_call": 0, "transitive_static_call": 1, "import_reference_only": 2}[
                    t.evidence_class
                ],
                t.display_name,
                t.provenance,
                tuple((e.snapshot, e.site.path, e.site.line) for e in t.import_evidence),
            ),
        )
    )
