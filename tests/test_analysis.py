from __future__ import annotations

import json
from importlib.resources import files

import jsonschema

from ripple.analysis import analyze
from ripple.models import Policy

SERVICE = "def total(x):\n    return x * 1.1\n"
API = (
    "from service import total as calculate\n\ndef checkout(x):\n    return calculate(x)\n"
    "\ndef endpoint(x):\n    return checkout(x)\n"
)


def two_commits(repo):
    repo.commit({"service.py": SERVICE, "api.py": API})
    repo.commit({"service.py": SERVICE.replace("1.1", "1.2")})


def test_two_commit_end_to_end_evidence_and_json_schema(repo):
    two_commits(repo)
    status = repo.git("status", "--porcelain=v1", "-z")
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert [(c.display_name, c.kind) for c in report.changes] == [
        ("service.total", "body_modified")
    ]
    assert [(i.display_name, i.depth) for i in report.impacts] == [
        ("api.checkout", 1),
        ("api.endpoint", 2),
    ]
    assert report.completeness.status == "complete_within_static_policy"
    direct = report.impacts[0]
    assert direct.provenance == "both"
    for evidence in direct.evidence_paths:
        step = evidence.steps[0]
        assert (step.caller, step.callee, step.site.path, step.site.line) == (
            "api.checkout",
            "service.total",
            "api.py",
            4,
        )
        assert step.resolver_rule == "python.imported-function-call-v1"
        assert evidence.origin_change_id == report.changes[0].change_id
    assert repo.git("status", "--porcelain=v1", "-z") == status
    assert report.to_json() == analyze(repo.root, "HEAD~1", "HEAD", Policy()).to_json()
    schema = json.loads(files("ripple").joinpath("schemas/report-v1.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(json.loads(report.to_json()), schema)


def test_working_tree_and_nested_cwd_do_not_contaminate_snapshots(repo):
    two_commits(repo)
    (repo.root / "service.py").write_text("def total(x): return 100\n")
    nested = repo.root / "nested"
    nested.mkdir()
    expected_status = repo.git("status", "--porcelain=v1", "-z")
    report = analyze(nested, "HEAD~1", "HEAD", Policy())
    assert [i.display_name for i in report.impacts] == ["api.checkout", "api.endpoint"]
    assert repo.git("status", "--porcelain=v1", "-z") == expected_status
    assert (repo.root / "service.py").read_text() == "def total(x): return 100\n"


def test_graph_direction_does_not_report_callees_as_impacts(repo):
    repo.commit({"m.py": "def helper(): return 1\ndef caller(): return helper()\n"})
    repo.commit({"m.py": "def helper(): return 1\ndef caller(): return helper() + 1\n"})
    assert not analyze(repo.root, "HEAD~1", "HEAD", Policy()).impacts


def test_whitespace_change_does_not_seed_impact(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    repo.commit({"m.py": "\n\ndef f():\n    return 1\n\ndef g():\n    return f()\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert not report.changes
    assert not report.impacts


def test_parse_failure_is_not_reported_as_symbol_deletion(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    repo.commit({"m.py": "def ???"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert not report.changes
    assert report.completeness.status == "partial"
    assert "parse_error" in report.completeness.reasons


def test_deleted_function_retains_old_evidence(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    repo.commit({"m.py": "def g(): return 2\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    change = next(c for c in report.changes if c.kind == "symbol_removed")
    assert change.display_name == "m.f"
    impact = next(i for i in report.impacts if i.display_name == "m.g")
    assert impact.provenance == "before_only"
    assert impact.evidence_paths[0].snapshot == "before"
    assert impact.evidence_paths[0].steps[0].site.line == 2


def test_new_caller_has_after_provenance(repo):
    repo.commit({"m.py": "def f(): return 1\n"})
    repo.commit({"m.py": "def f(): return 2\ndef g(): return f()\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert report.impacts[0].provenance == "after_only"


def test_cycles_terminate_and_do_not_readd_seed(repo):
    repo.commit({"m.py": "def a(): return b()\ndef b(): return a()\n"})
    repo.commit({"m.py": "def a(): return b() + 1\ndef b(): return a()\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert [i.display_name for i in report.impacts] == ["m.b"]


def test_depth_cap_is_visible(repo):
    two_commits(repo)
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy(max_depth=1))
    assert [i.display_name for i in report.impacts] == ["api.checkout"]
    assert report.completeness.truncated
    assert "max_depth" in report.completeness.reasons


def test_node_cap_is_visible(repo):
    two_commits(repo)
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy(max_nodes=1))
    assert len(report.impacts) == 1
    assert report.completeness.truncated
    assert "max_nodes" in report.completeness.reasons


def test_same_tree_with_duplicate_names_is_no_changes(repo):
    repo.commit({"m.py": "def f(): return 1\ndef f(): return 2\n"})
    report = analyze(repo.root, "HEAD", "HEAD", Policy())
    assert not report.changes
    assert not report.impacts


def test_multiple_origins_deduplicate_impact_counts(repo):
    repo.commit({"m.py": "def a(): return 1\ndef b(): return 1\ndef g(): return a() + b()\n"})
    repo.commit({"m.py": "def a(): return 2\ndef b(): return 2\ndef g(): return a() + b()\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert len(report.impacts) == report.summary["impacted_direct_count"] == 1
    assert len(report.impacts[0].origin_change_ids) == 2
    assert len(report.impacts[0].evidence_paths) == 4


def test_shortest_canonical_path(repo):
    source = (
        "def f(): return 1\ndef a(): return f()\ndef b(): return f()\ndef z(): return b() + a()\n"
    )
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    z = next(i for i in report.impacts if i.display_name == "m.z")
    assert z.depth == 2
    assert all(path.steps[0].callee == "m.a" for path in z.evidence_paths)


def test_no_target_execution_even_with_hostile_top_level(repo):
    hostile = "from pathlib import Path\nPath('EXECUTED').write_text('bad')\n"
    repo.commit({"service.py": hostile + SERVICE, "api.py": API})
    repo.commit({"service.py": hostile + SERVICE.replace("1.1", "1.2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert not (repo.root / "EXECUTED").exists()
    assert len(report.impacts) == 2
    assert report.completeness.status == "partial"


def test_method_change_has_lexical_dispatch_note_and_exact_evidence(repo):
    source = "class C:\n    def f(self): return 1\n    def g(self): return self.f()\n"
    repo.commit({"pkg/m.py": source})
    repo.commit({"pkg/m.py": source.replace("return 1", "return 2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert [c.display_name for c in report.changes] == ["pkg.m.C.f"]
    assert [i.display_name for i in report.impacts] == ["pkg.m.C.g"]
    for evidence in report.impacts[0].evidence_paths:
        assert evidence.steps[0].site.line == 3
        assert "runtime subclass overrides" in evidence.steps[0].explanation


def test_nested_function_change_reaches_lexical_caller(repo):
    source = "def outer():\n    def inner(): return 1\n    return inner()\n"
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    assert any(c.display_name == "m.outer.inner" for c in report.changes)
    assert any(i.display_name == "m.outer" for i in report.impacts)
