from __future__ import annotations

from ripple.models import Report


def safe_text(text: str) -> str:
    """Escape terminal controls, including bidi format controls and invalid path bytes."""
    import unicodedata

    return "".join(
        char if unicodedata.category(char) not in {"Cc", "Cf", "Cs"} else ascii(char)[1:-1]
        for char in text
    )


def render_text(report: Report) -> str:
    lines = [
        "RIPPLE - potential change impact",
        f"Trees: {report.comparison.base_tree} -> {report.comparison.head_tree}",
        "Mode: Python static analysis; no target code execution",
        f"Source roots: before {', '.join(report.comparison.base_source_roots)}; "
        f"after {', '.join(report.comparison.head_source_roots)}",
        "",
        "CHANGES",
    ]
    for change in report.changes:
        site = change.after or change.before
        assert site is not None
        lines.append(f"  {change.kind}: {change.display_name} ({site.path}:{site.line})")
        if change.before and change.after and change.kind == "symbol_relocated":
            caveat = (
                "; Git rename evidence is heuristic"
                if change.match_rule.startswith("git_file_rename_")
                else ""
            )
            lines.append(
                f"    before {change.before_display_name} "
                f"at {change.before.path}:{change.before.line}"
                f" -> after {change.after_display_name} at {change.after.path}:{change.after.line}"
                f" ({change.match_rule}{caveat})"
            )
    if not report.changes:
        lines.append("  No semantic definition changes found between selected trees.")
    if report.match_candidates:
        lines.extend(["", "RELOCATION CANDIDATES (no identity merge)"])
        for candidate in report.match_candidates:
            lines.append(
                f"  {', '.join(candidate.before_names)} -> {', '.join(candidate.after_names)} "
                f"({candidate.match_rule})"
            )
            lines.append(f"    {candidate.explanation}")
    lines.extend(["", "POTENTIAL CALLER IMPACT"])
    for impact in report.impacts:
        lines.append(
            f"  {impact.display_name}: {impact.category}, depth {impact.depth}, "
            f"{impact.provenance} [resolved]"
        )
        if impact.additional_paths_possible:
            lines.append("    Additional paths or cycles exist; showing canonical shortest paths.")
        for evidence in impact.evidence_paths:
            lines.append(f"    Change {evidence.origin_change_id} [{evidence.snapshot}]")
            for step in evidence.steps:
                lines.append(
                    f"      {step.caller} --CALLS--> {step.callee} "
                    f"at {step.site.path}:{step.site.line}:{step.site.column} "
                    f"({step.resolver_rule})"
                )
                if step.explanation:
                    lines.append(f"        {step.explanation}")
    if not report.impacts:
        lines.append("  No resolved downstream callers found within indexed scope.")
    c = report.completeness
    lines.extend(
        [
            "",
            "COMPLETENESS",
            f"  {c.status}; {c.parsed_files} parsed files across both sides; "
            f"{c.skipped_files} skipped; {c.unresolved_references} unresolved references.",
            f"  Limits: depth {report.policy.max_depth}, nodes {report.policy.max_nodes}; "
            f"truncated: {str(c.truncated).lower()}.",
        ]
    )
    for diagnostic in report.diagnostics:
        lines.append(
            f"  [{diagnostic['snapshot']}] {diagnostic['code']}: "
            f"{diagnostic['path']}:{diagnostic['line']} - {diagnostic['message']}"
        )
    lines.append("  Static evidence does not guarantee runtime behavior or safety.")
    return "\n".join(safe_text(line) for line in lines) + "\n"
