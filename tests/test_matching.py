from __future__ import annotations

import json
from importlib.resources import files

import jsonschema
import pytest

from ripple.analysis import analyze
from ripple.frontend import build_snapshot
from ripple.git_source import GitSource, SourceFile
from ripple.matching import compare
from ripple.models import FileChange, Policy
from ripple.output import render_text


def read(repo):
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    schema = json.loads(files("ripple").joinpath("schemas/report-v1.schema.json").read_text())
    jsonschema.validate(json.loads(report.to_json()), schema)
    return report


def test_exact_git_file_rename_preserves_old_and_new_identity(repo):
    repo.commit({"old.py": "def f(): return 1\ndef caller(): return f()\n"})
    repo.commit({"old.py": None, "new.py": "def f(): return 1\ndef caller(): return f()\n"})
    report = read(repo)
    assert [(f.status, f.before_path, f.after_path, f.similarity) for f in report.file_changes] == [
        ("renamed", "old.py", "new.py", 100)
    ]
    assert all(c.kind == "symbol_relocated" for c in report.changes)
    assert len(report.symbol_matches) == 3
    f = next(c for c in report.changes if c.after_display_name == "new.f")
    assert f.before_display_name == "old.f"
    assert f.before.path == "old.py" and f.after.path == "new.py"
    assert "git_file_rename_100" in f.match_rule
    assert len(report.impacts) == 1
    caller = report.impacts[0]
    assert caller.provenance == "both"
    assert caller.before_display_name == "old.caller"
    assert caller.after_display_name == "new.caller"
    assert caller.before.path == "old.py" and caller.after.path == "new.py"
    assert [(p.snapshot, p.steps[0].site.path) for p in caller.evidence_paths] == [
        ("after", "new.py"),
        ("before", "old.py"),
    ]
    assert "Git rename evidence is heuristic" in render_text(report)
    schema = json.loads(files("ripple").joinpath("schemas/report-v1.schema.json").read_text())
    jsonschema.validate(json.loads(report.to_json()), schema)


def test_copy_is_added_not_identity_merge(repo):
    source = "def f(): return 1\ndef caller(): return f()\n"
    repo.commit({"original.py": source})
    repo.commit({"copy.py": source})
    report = read(repo)
    file = report.file_changes[0]
    assert (file.status, file.similarity) == ("copied", 100)
    assert all(c.kind == "symbol_added" for c in report.changes)
    assert {c.after.path for c in report.changes} == {"copy.py"}
    assert not report.match_candidates
    assert all("git_file_rename" not in m.match_rule for m in report.symbol_matches)
    assert [(i.display_name, i.provenance) for i in report.impacts] == [
        ("copy.caller", "after_only")
    ]


def test_same_file_function_rename_is_explicit_candidate_with_separate_graphs(repo):
    repo.commit({"m.py": "def old_api(x): return x + 1\ndef g(x): return old_api(x)\n"})
    repo.commit({"m.py": "def new_api(x): return x + 1\ndef g(x): return new_api(x)\n"})
    report = read(repo)
    candidate = report.match_candidates[0]
    assert candidate.before_names == ("m.old_api",)
    assert candidate.after_names == ("m.new_api",)
    assert candidate.match_rule == "unique_structural_candidate-v1"
    assert {c.kind for c in report.changes if c.display_name in {"m.old_api", "m.new_api"}} == {
        "symbol_added",
        "symbol_removed",
    }
    assert not any(c.kind == "symbol_relocated" for c in report.changes)
    g = next(i for i in report.impacts if i.display_name == "m.g")
    assert g.provenance == "both"
    assert {p.steps[0].callee for p in g.evidence_paths} == {"m.old_api", "m.new_api"}
    assert len(g.origin_change_ids) == 2


def test_ambiguous_cross_file_move_never_picks_one_target(repo):
    repo.commit({"a.py": "def f(x): return x + 1\ndef g(x): return x + 1\n"})
    repo.commit({"a.py": None, "b.py": "# distinct\ndef h(x): return x + 1\n"})
    report = read(repo)
    candidate = report.match_candidates[0]
    assert candidate.match_rule == "ambiguous_structural_candidate-v1"
    assert candidate.before_names == ("a.f", "a.g")
    assert candidate.after_names == ("b.h",)
    assert "ambiguous_relocation" in report.completeness.reasons
    assert not any(
        c.kind == "symbol_relocated" and c.after_display_name == "b.h" for c in report.changes
    )


def test_duplicate_symbols_are_not_matched_by_line_or_occurrence(repo):
    repo.commit({"m.py": "def f(): return 1\ndef f(): return 2\n"})
    repo.commit({"m.py": "\ndef f(): return 2\ndef f(): return 1\n"})
    report = read(repo)
    assert "ambiguous_symbol_match" in report.completeness.reasons
    assert all(c.match_rule == "unmatched-v1" for c in report.changes)
    assert len(report.changes) == 4


def test_unchanged_caller_file_move_is_deduplicated_with_locations(repo):
    caller = "from service import f\ndef g(): return f()\n"
    repo.commit({"service.py": "def f(): return 1\n", "old.py": caller})
    repo.commit({"service.py": "def f(): return 2\n", "old.py": None, "new.py": caller})
    report = read(repo)
    assert [(i.display_name, i.provenance) for i in report.impacts] == [("new.g", "both")]
    assert report.impacts[0].before.path == "old.py"
    assert report.impacts[0].after.path == "new.py"


@pytest.mark.parametrize("kind", ["function", "class", "module"])
def test_docstring_only_is_visible_without_caller_propagation(repo, kind):
    source = {
        "function": 'def f():\n    "old"\n    return 1\ndef g(): return f()\n',
        "class": 'class C:\n    "old"\n    def f(self): return 1\n',
        "module": '"old"\ndef f(): return 1\ndef g(): return f()\n',
    }[kind]
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace('"old"', '"new"')})
    report = read(repo)
    assert [c.kind for c in report.changes] == ["docstring_modified"]
    assert not report.impacts


def test_nested_body_change_does_not_seed_enclosing_body_hash(repo):
    source = "def outer():\n    def inner(): return 1\n    return inner()\n"
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    report = read(repo)
    assert [(c.display_name, c.kind) for c in report.changes] == [
        ("m.outer.inner", "body_modified")
    ]
    assert [i.display_name for i in report.impacts] == ["m.outer"]


def test_signature_and_decorators_have_separate_facts(repo):
    repo.commit({"m.py": "def deco(f): return f\ndef f(x): return x\ndef g(): return f(1)\n"})
    repo.commit(
        {"m.py": "def deco(f): return f\n@deco\ndef f(x, y=1): return x\ndef g(): return f(1)\n"}
    )
    report = read(repo)
    assert {c.kind for c in report.changes if c.display_name == "m.f"} == {
        "signature_modified",
        "decorators_modified",
    }
    assert next(i for i in report.impacts if i.display_name == "m.g").provenance == "before_only"


def test_class_bases_and_module_imports_are_warnings_not_call_seeds(repo):
    repo.commit(
        {"m.py": "import os\nclass A: pass\nclass B: pass\nclass C(A): pass\ndef g(): return C()\n"}
    )
    repo.commit(
        {
            "m.py": "import sys\nclass A: pass\nclass B: pass\nclass C(B): pass\n"
            "def g(): return C()\n"
        }
    )
    report = read(repo)
    assert any(c.kind == "bases_modified" and c.display_name == "m.C" for c in report.changes)
    assert any(c.kind == "imports_modified" for c in report.changes)
    assert not report.impacts


def test_top_level_side_effect_change_is_visible_and_never_executed(repo):
    source = "from pathlib import Path\nPath('EXECUTED').write_text('one')\ndef f(): return 1\n"
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("'one'", "'two'")})
    report = read(repo)
    assert [c.kind for c in report.changes] == ["module_body_modified"]
    assert not report.impacts
    assert not (repo.root / "EXECUTED").exists()


def test_multiple_paths_and_cycle_have_visible_notice(repo):
    source = "def f(): return z()\ndef a(): return f()\ndef b(): return f()\n"
    source += "def z(): return b() + a()\n"
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return z()", "return z() + 1")})
    report = read(repo)
    z = next(i for i in report.impacts if i.display_name == "m.z")
    assert z.additional_paths_possible
    assert all(len(p.steps) == 2 and p.steps[0].callee == "m.a" for p in z.evidence_paths)
    assert "Additional paths or cycles exist" in render_text(report)
    assert all(i.display_name != "m.f" for i in report.impacts)


def test_failed_renamed_destination_does_not_manufacture_deletion():
    before = build_snapshot("a" * 40, [SourceFile("old.py", b"def f(): return 1\n")], [])
    after = build_snapshot("b" * 40, [SourceFile("new.py", b"def ???")], [])
    result = compare(
        before, after, (FileChange("renamed", "old.py", "new.py", "a" * 40, "b" * 40, 90),)
    )
    assert not result.changes


def test_raw_diff_handles_unicode_spaces_tabs_and_optionlike_names(repo):
    paths = ["-option.py", "with space.py", "café.py"]
    repo.commit({p: "def f(): return 1\n" for p in paths})
    repo.commit({p: "def f(): return 2\n" for p in paths})
    source = GitSource(repo.root)
    diff = source.diff(source.resolve("HEAD~1"), source.resolve("HEAD"))
    assert {f.before_path for f in diff} == set(paths)
    assert all(f.status == "modified" and f.before_blob != f.after_blob for f in diff)


@pytest.mark.parametrize("threshold", [49, 101])
def test_invalid_rename_threshold_is_rejected(repo, threshold):
    from ripple.errors import RippleError

    repo.commit({"m.py": "def f(): pass\n"})
    with pytest.raises(RippleError, match="threshold"):
        analyze(repo.root, "HEAD", "HEAD", Policy(), rename_threshold=threshold)


def test_similarity_rename_with_body_change_records_both_facts(repo):
    source = "# stable context\n" * 30 + "def f(): return 1\ndef g(): return f()\n"
    repo.commit({"old.py": source})
    repo.commit({"old.py": None, "new.py": source.replace("return 1", "return 2")})
    report = read(repo)
    file = next(f for f in report.file_changes if f.status == "renamed")
    assert 90 <= file.similarity < 100
    assert {c.kind for c in report.changes if c.display_name == "new.f"} == {
        "symbol_relocated",
        "body_modified",
    }
    assert len(report.impacts) == 1
    assert report.impacts[0].provenance == "both"
    assert report.to_json() == read(repo).to_json()


def test_rename_threshold_100_leaves_modified_move_as_candidates(repo):
    source = "# stable context\n" * 30 + "def f(): return 1\ndef g(): return f()\n"
    repo.commit({"old.py": source})
    repo.commit({"old.py": None, "new.py": source.replace("return 1", "return 2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy(), rename_threshold=100)
    assert {f.status for f in report.file_changes} == {"added", "deleted"}
    assert not any(c.kind == "symbol_relocated" for c in report.changes)
    assert any(
        c.before_names == ("old.g",) and c.after_names == ("new.g",)
        for c in report.match_candidates
    )
    assert {i.provenance for i in report.impacts} == {"before_only", "after_only"}


def test_same_tree_raw_diff_is_empty(repo):
    repo.commit({"m.py": "def f(): pass\n"})
    source = GitSource(repo.root)
    tree = source.resolve("HEAD")
    assert source.diff(tree, tree) == ()
    assert not analyze(repo.root, "HEAD", "HEAD", Policy()).file_changes


def test_diff_never_runs_configured_external_diff_or_textconv(repo):
    repo.commit({"m.py": "def f(): return 1\n", ".gitattributes": "*.py diff=hostile\n"})
    repo.git("config", "diff.external", "ripple-command-that-must-not-run")
    repo.git("config", "diff.hostile.textconv", "ripple-textconv-that-must-not-run")
    repo.commit({"m.py": "def f(): return 2\n"})
    report = read(repo)
    assert [c.kind for c in report.changes] == ["body_modified"]


def test_docstring_addition_preserves_runtime_body(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    repo.commit({"m.py": 'def f():\n    "documentation"\n    return 1\ndef g(): return f()\n'})
    report = read(repo)
    assert [c.kind for c in report.changes] == ["docstring_modified"]
    assert not report.impacts


def test_body_normalization_does_not_mutate_ast_or_resolution():
    import ast

    from ripple.python_extract import body_digest

    tree = ast.parse("if flag:\n    def f(): return 1\n    value = f()\n")
    original = ast.dump(tree)
    body_digest(tree.body)
    assert ast.dump(tree) == original


def test_module_mapping_change_retains_same_path_identity(repo):
    source = "def f(): return 1\ndef g(): return f()\n"
    repo.commit({"src/m.py": source, "pyproject.toml": '[tool.ripple]\nsource_roots=["src"]\n'})
    repo.commit({"pyproject.toml": '[tool.ripple]\nsource_roots=["."]\n'})
    report = read(repo)
    assert any(
        c.before_display_name == "m.f"
        and c.after_display_name == "src.m.f"
        and c.match_rule == "unique_path_qualname_kind-v1"
        for c in report.changes
    )
    assert report.impacts[0].before.path == report.impacts[0].after.path == "src/m.py"
    assert "Git rename evidence is heuristic" not in render_text(report)


def test_unchanged_duplicates_do_not_become_changes_when_another_file_changes(repo):
    duplicate = "def f(): return 1\ndef f(): return 2\n"
    repo.commit({"m.py": duplicate, "other.py": "def g(): return 1\n"})
    repo.commit({"other.py": "def g(): return 2\n"})
    report = read(repo)
    assert [(c.display_name, c.kind) for c in report.changes] == [("other.g", "body_modified")]
    assert not report.match_candidates


def test_whitespace_shifted_duplicate_definitions_do_not_create_change_facts(repo):
    source = "def f(): return 1\ndef f(): return 2\n"
    repo.commit({"m.py": source})
    repo.commit({"m.py": "\n\n" + source})
    report = read(repo)
    assert not report.changes
    assert not report.match_candidates


def test_same_tree_duplicates_have_no_relocation_candidates(repo):
    repo.commit({"m.py": "def f(): return 1\ndef f(): return 2\n"})
    report = analyze(repo.root, "HEAD", "HEAD", Policy())
    assert not report.changes
    assert not report.match_candidates
    assert not any(d["snapshot"] == "comparison" for d in report.diagnostics)
