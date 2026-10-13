"""Read bounded report data and validate evidence relationships without executing code."""

from __future__ import annotations

import json
from pathlib import Path

from ripple.cache_codec import MAX_PAYLOAD, typed_value
from ripple.models import Report


def validate_report(report: Report) -> None:
    if report.schema != "ripple.report.v1":
        raise ValueError("Unsupported report schema")
    origins = {c.change_id: c for c in report.changes}
    if len(origins) != len(report.changes):
        raise ValueError("Duplicate change identity")
    if any(c.before is None and c.after is None for c in report.changes):
        raise ValueError("Change requires a source location")
    if len({i.symbol_id for i in report.impacts}) != len(report.impacts):
        raise ValueError("Duplicate impact identity")
    for impact in report.impacts:
        if impact.resolution_state != "resolved" or not impact.evidence_paths:
            raise ValueError("Impact requires resolved evidence")
        found = {p.origin_change_id for p in impact.evidence_paths}
        if found != set(impact.origin_change_ids) or not found <= origins.keys():
            raise ValueError("Invalid evidence origin")
        if impact.depth != min(len(p.steps) for p in impact.evidence_paths):
            raise ValueError("Invalid impact depth")
        if impact.category != ("direct_caller" if impact.depth == 1 else "transitive_caller"):
            raise ValueError("Invalid impact category")
        sides = {p.snapshot for p in impact.evidence_paths}
        expected = "both" if len(sides) == 2 else next(iter(sides)) + "_only"
        if impact.provenance != expected:
            raise ValueError("Invalid impact provenance")
        for path in impact.evidence_paths:
            if not path.steps or len(path.steps) > report.policy.max_depth:
                raise ValueError("Invalid evidence length")
            tree = (
                report.comparison.base_tree
                if path.snapshot == "before"
                else (report.comparison.head_tree)
            )
            if path.tree != tree:
                raise ValueError("Evidence tree differs from comparison")
            name = (
                impact.before_display_name
                if path.snapshot == "before"
                else (impact.after_display_name)
            )
            if path.steps[0].caller != (name or impact.display_name):
                raise ValueError("Evidence does not begin at impacted symbol")
            for step in path.steps:
                if step.edge_type != "CALLS" or not step.resolver_rule or step.site.line < 1:
                    raise ValueError("Invalid call evidence")
            if any(a.callee != b.caller for a, b in zip(path.steps, path.steps[1:], strict=False)):
                raise ValueError("Disconnected evidence path")
            change = origins[path.origin_change_id]
            target = (
                change.before_display_name
                if path.snapshot == "before"
                else (change.after_display_name)
            )
            if path.steps[-1].callee != (target or change.display_name):
                raise ValueError("Evidence does not end at changed symbol")
    expected_counts = {
        "impacted_direct_count": sum(i.depth == 1 for i in report.impacts),
        "impacted_transitive_count": sum(i.depth > 1 for i in report.impacts),
        "change_fact_count": len(report.changes),
        "candidate_impact_count": 0,
        "changed_symbol_count": len(
            {c.after_symbol_id or c.before_symbol_id for c in report.changes}
        ),
    }
    if any(report.summary.get(key) != value for key, value in expected_counts.items()):
        raise ValueError("Report counts disagree with findings")
    if len(report.impacts) > report.policy.max_nodes:
        raise ValueError("Impact inventory exceeds policy")
    if any(len(i.evidence_paths) > report.policy.max_paths_per_target for i in report.impacts):
        raise ValueError("Evidence inventory exceeds policy")
    for group in report.impact_groups:
        if (
            group.count != sum(i.category == group.category for i in report.impacts)
            or group.category not in {"direct_caller", "transitive_caller"}
            or group.count <= 20
            or group.displayed_count != 20
        ):
            raise ValueError("Invalid impact group counts")
    strong = sum(t.evidence_class != "import_reference_only" for t in report.related_tests)
    if report.summary.get("related_test_count", strong) != strong:
        raise ValueError("Test counts disagree with findings")
    for test in report.related_tests:
        if not set(test.origin_change_ids) <= origins.keys():
            raise ValueError("Invalid test origin")
        if test.evidence_class == "import_reference_only":
            if test.evidence_paths or not test.import_evidence:
                raise ValueError("Invalid import-only evidence")
            for evidence in test.import_evidence:
                tree = (
                    report.comparison.base_tree
                    if evidence.snapshot == "before"
                    else (report.comparison.head_tree)
                )
                if evidence.tree != tree or not evidence.resolver_rule:
                    raise ValueError("Invalid import provenance")
                if not set(evidence.origin_change_ids) <= origins.keys():
                    raise ValueError("Invalid import origin")
        elif test.evidence_class in {"direct_static_call", "transitive_static_call"}:
            if not any(
                i.symbol_id == test.symbol_id and i.evidence_paths == test.evidence_paths
                for i in report.impacts
            ):
                raise ValueError("Test evidence is absent from impact inventory")
        else:
            raise ValueError("Unknown test evidence class")


def decode_report(payload: str) -> Report:
    if len(payload.encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("Report exceeds input limit")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate report field")
            result[key] = value
        return result

    report: Report = typed_value(json.loads(payload, object_pairs_hook=unique), Report)
    validate_report(report)
    return report


def read_report(path: Path) -> Report:
    with path.open("rb") as stream:
        data = stream.read(MAX_PAYLOAD + 1)
    if len(data) > MAX_PAYLOAD:
        raise ValueError("Report exceeds input limit")
    return decode_report(data.decode("utf-8"))
