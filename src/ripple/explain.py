"""Select an impacted symbol without guessing between ambiguous names."""

from __future__ import annotations

from dataclasses import replace

from ripple.errors import RippleError
from ripple.models import Impact, Report


def select_impact(
    report: Report,
    name: str | None = None,
    symbol_id: str | None = None,
    location: str | None = None,
) -> Impact:
    matches = list(report.impacts)
    if symbol_id is not None:
        matches = [i for i in matches if i.symbol_id == symbol_id]
    elif name is not None:
        matches = [
            i
            for i in matches
            if name
            in (
                i.display_name,
                i.before_display_name,
                i.after_display_name,
            )
        ]
    elif location is not None:
        try:
            path, value = location.rsplit(":", 1)
            line = int(value)
            if line < 1:
                raise ValueError
        except ValueError as exc:
            raise RippleError("RPL-EXPLAIN-001", "Use a repository-relative path:line.", 2) from exc
        matches = [
            i
            for i in matches
            if any(
                s is not None and s.path == path and s.line <= line <= s.end_line
                for s in (i.before, i.after)
            )
        ]
    else:
        raise RippleError("RPL-EXPLAIN-001", "Choose a name, --symbol-id or --path.", 2)
    if not matches:
        raise RippleError("RPL-EXPLAIN-002", "No matching impacted symbol in this report.", 2)
    if len(matches) != 1:
        choices = "; ".join(f"{i.display_name} --symbol-id {i.symbol_id}" for i in matches)
        raise RippleError("RPL-EXPLAIN-003", f"Ambiguous symbol. Choose: {choices}", 2)
    return matches[0]


def explanation(report: Report, impact: Impact) -> Report:
    """A report projection retaining original coverage, policy and provenance."""
    changes = tuple(c for c in report.changes if c.change_id in impact.origin_change_ids)
    tests = tuple(t for t in report.related_tests if t.symbol_id == impact.symbol_id)
    return replace(
        report,
        changes=changes,
        impacts=(impact,),
        related_tests=tests,
        file_changes=(),
        symbol_matches=(),
        match_candidates=(),
        impact_groups=(),
        summary={
            "changed_symbol_count": len({c.after_symbol_id or c.before_symbol_id for c in changes}),
            "change_fact_count": len(changes),
            "impacted_direct_count": int(impact.depth == 1),
            "impacted_transitive_count": int(impact.depth > 1),
            "candidate_impact_count": 0,
            "relocation_candidate_count": 0,
            "related_test_count": len(tests),
            "related_test_import_module_count": 0,
        },
    )
