from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from importlib.resources import files

import jsonschema
import pytest

from ripple.analysis import analyze, impacts_for
from ripple.cache import Cache
from ripple.errors import RippleError
from ripple.explain import explanation, select_impact
from ripple.frontend import build_snapshot
from ripple.git_source import SourceFile
from ripple.models import Policy
from ripple.output import render_text
from ripple.reports import decode_report, validate_report


def graph_pair(source: str):
    before = build_snapshot("a" * 40, [SourceFile("m.py", source.encode())], [])
    after = build_snapshot(
        "b" * 40, [SourceFile("m.py", source.replace("return 1", "return 2").encode())], []
    )
    from ripple.analysis import changes_between

    return before, after, changes_between(before, after)


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "ripple", *map(str, args)], capture_output=True, timeout=30
    )


def fixture_report(repo):
    source = "def f(): return 1\ndef g(): return f()\ndef h(): return g()\n"
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    return analyze(repo.root, "HEAD~1", "HEAD", Policy())


def test_dense_graph_work_limit_and_unbounded_parity():
    source = "def f(): return 1\n" + "".join(f"def c{i}(): return f()\n" for i in range(80))
    before, after, changes = graph_pair(source)
    limited, reasons = impacts_for(changes, before, after, Policy(max_edges=85))
    full, full_reasons = impacts_for(changes, before, after, Policy())
    assert "max_edges" in reasons
    assert 0 < len(limited) < len(full) == 80
    assert not full_reasons
    assert all(i.evidence_paths for i in limited)


def test_query_deadline_is_explicit_and_retains_only_valid_paths(monkeypatch):
    before, after, changes = graph_pair("def f(): return 1\ndef g(): return f()\n")
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr("ripple.analysis.time.monotonic", lambda: next(ticks, 2.0))
    impacts, reasons = impacts_for(changes, before, after, Policy(query_timeout_ms=1))
    assert impacts == ()
    assert reasons == ("query_timeout",)


def test_many_origins_have_capped_evidence_without_duplicate_counts():
    before, after, changes = graph_pair(
        "def f(): return 1\ndef a(): return 1\ndef g(): return f() + a()\n"
    )
    impacts, reasons = impacts_for(changes, before, after, Policy(max_paths_per_target=1))
    assert len(impacts) == 1
    assert len(impacts[0].evidence_paths) == 1
    assert len(impacts[0].origin_change_ids) == 1
    assert impacts[0].additional_paths_possible
    assert "max_paths_per_target" in reasons


@pytest.mark.parametrize(
    "limits",
    [
        {"max_edges": 0},
        {"query_timeout_ms": True},
        {"max_paths_per_target": -1},
        {"include_candidates": True},
    ],
)
def test_invalid_policy_fails_before_work(limits):
    with pytest.raises(ValueError):
        Policy(**limits)


def test_related_tests_call_import_and_unknown_evidence_stay_separate(repo):
    service = "def f(): return 1\ndef wrapper(): return f()\n"
    tests = (
        "from service import f, wrapper\n"
        "def test_direct(): return f()\n"
        "async def test_indirect(): return wrapper()\n"
        "class TestCalls:\n    def test_method(self): return f()\n"
        "def helper(): return f()\n"
        "def test_unknown(obj): return obj.f()\n"
        "def outer():\n    def test_nested(): return f()\n"
    )
    repo.commit(
        {
            "service.py": service,
            "tests/test_service.py": tests,
            "tests/test_unused.py": "from service import f\ndef test_nothing(): pass\n",
            "app.py": "from service import f\ndef test_business_name(): return f()\n",
        }
    )
    repo.commit({"service.py": service.replace("return 1", "return 2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    strong = [t for t in report.related_tests if t.evidence_class != "import_reference_only"]
    weak = [t for t in report.related_tests if t.evidence_class == "import_reference_only"]
    assert len(strong) == 3
    assert {t.evidence_class for t in strong} == {"direct_static_call", "transitive_static_call"}
    assert len(weak) == 2
    assert all(t.provenance == "both" and len(t.import_evidence) == 2 for t in weak)
    assert report.summary["related_test_count"] == 3
    assert report.summary["related_test_import_module_count"] == 2
    assert report.completeness.unresolved_references == 2
    assert not any("test_unknown" in t.display_name for t in strong)
    assert not any("test_business_name" in t.display_name for t in strong)
    assert decode_report(report.to_json()) == report
    assert not (repo.root / "EXECUTED").exists()


def test_text_groups_high_degree_callers_but_json_keeps_every_path(repo):
    source = "def f(): return 1\n" + "".join(f"def c{i}(): return f()\n" for i in range(25))
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert len(report.impacts) == 25
    assert report.impact_groups[0].count == 25
    assert "showing 20 of 25" in render_text(report)
    assert render_text(report).count("depth 1") == 20
    assert len(json.loads(report.to_json())["impacts"]) == 25


@pytest.mark.parametrize("damage", ["origin", "tree", "chain", "count", "category"])
def test_report_rejects_disconnected_or_miscounted_evidence(repo, damage):
    data = json.loads(fixture_report(repo).to_json())
    impact = data["impacts"][-1]
    if damage == "origin":
        impact["evidence_paths"][0]["origin_change_id"] = "unknown"
    elif damage == "tree":
        impact["evidence_paths"][0]["tree"] = "0" * 40
    elif damage == "chain":
        impact["evidence_paths"][0]["steps"][0]["callee"] = "wrong"
    elif damage == "count":
        data["summary"]["impacted_direct_count"] += 1
    else:
        impact["category"] = "direct_caller"
    with pytest.raises(ValueError):
        decode_report(json.dumps(data))


def test_report_old_additive_fields_and_duplicate_json_keys(repo):
    report = fixture_report(repo)
    data = json.loads(report.to_json())
    for key in ("related_tests", "impact_groups"):
        del data[key]
    for key in ("max_edges", "max_paths_per_target", "query_timeout_ms"):
        del data["policy"][key]
    assert decode_report(json.dumps(data)).impacts == report.impacts
    with pytest.raises(ValueError, match="Duplicate"):
        decode_report('{"schema": "ripple.report.v1", "schema": "wrong"}')


def test_explain_saved_file_is_portable_and_schema_valid(repo, tmp_path):
    report = fixture_report(repo)
    path = tmp_path / "report.json"
    path.write_text(report.to_json())
    result = run_cli("explain", "m.h", "--report", path, "--format", "json")
    assert result.returncode == 0, result.stderr
    selected = decode_report(result.stdout.decode())
    assert [i.display_name for i in selected.impacts] == ["m.h"]
    assert len(selected.impacts[0].evidence_paths[0].steps) == 2
    schema = json.loads(files("ripple").joinpath("schemas/report-v1.schema.json").read_text())
    jsonschema.validate(json.loads(result.stdout), schema)
    assert run_cli("explain", "unknown", "--report", path).returncode == 2


def test_explain_ambiguity_ids_locations_and_terminal_safety(repo):
    report = fixture_report(repo)
    impact = report.impacts[0]
    ambiguous = replace(report, impacts=(impact, replace(impact, symbol_id="other")))
    with pytest.raises(RippleError, match="Ambiguous"):
        select_impact(ambiguous, "m.g")
    assert select_impact(ambiguous, symbol_id=impact.symbol_id) == impact
    assert select_impact(report, location="m.py:3").display_name == "m.h"
    with pytest.raises(RippleError):
        select_impact(report, location="m.py:bad")
    selected = explanation(report, impact)
    validate_report(selected)
    assert "--CALLS-->" in render_text(selected)


def test_explain_cached_analysis_stale_corrupt_and_exact_analysis_id(repo):
    report = fixture_report(repo)
    cache = Cache(repo.root)
    analysis_id = cache.status()["last_analysis"]["analysis_id"]
    assert cache.load_report(analysis_id) == report
    assert run_cli("explain", "m.g", "--repo", repo.root).returncode == 0
    repo.commit({"new.py": "# new tree\n"})
    stale = run_cli("explain", "m.g", "--repo", repo.root)
    assert stale.returncode == 2
    assert b"RPL-EXPLAIN-004" in stale.stderr
    historical = run_cli(
        "explain",
        "m.g",
        "--repo",
        repo.root,
        "--analysis",
        analysis_id,
        "--allow-stale",
        "--format",
        "json",
    )
    assert historical.returncode == 0
    assert json.loads(historical.stdout)["comparison"]["head_tree"] == report.comparison.head_tree
    with cache.connection() as connection, connection:
        connection.execute('UPDATE analysis_runs SET checksum="bad"')
    broken = run_cli("explain", "m.g", "--repo", repo.root, "--allow-stale")
    assert broken.returncode == 2
    assert broken.stdout == b""
    assert b"RPL-REPORT-001" in broken.stderr


def test_cli_work_cap_strict_and_invalid_query_flags(repo):
    fixture_report(repo)
    result = run_cli(
        "analyze", "--repo", repo.root, "--max-edges", "1", "--strict", "--format", "json"
    )
    assert result.returncode == 4
    assert "max_edges" in json.loads(result.stdout)["completeness"]["reasons"]
    assert run_cli("analyze", "--query-timeout-ms", "0").returncode == 2
    assert run_cli("analyze", "--query-timeout-ms", "9" * 300).returncode == 2
    assert run_cli("analyze", "--max-paths-per-target", "-1").returncode == 2
