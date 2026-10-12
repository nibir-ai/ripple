from __future__ import annotations

import pytest

from ripple.frontend import build_snapshot
from ripple.git_source import SourceFile


def graph(source: str):
    result = build_snapshot("a" * 40, [SourceFile("m.py", source.encode())], [])
    symbols = {s.id: s.display_name for s in result.symbols}
    return result, {
        (symbols[e.source_id], symbols[e.target_id]) for e in result.edges if e.edge_type == "CALLS"
    }


def test_nested_definition_and_closure_resolve_nearest_binding():
    _, calls = graph(
        "def f(): pass\ndef outer():\n    def f(): pass\n"
        "    def inner(): return f()\n    return inner()\n"
    )
    assert calls == {("m.outer.inner", "m.outer.f"), ("m.outer", "m.outer.inner")}


def test_nested_recursion_and_outer_reference():
    _, calls = graph(
        "def outer():\n    def inner():\n        inner()\n        outer()\n    inner()\n"
    )
    assert calls == {
        ("m.outer.inner", "m.outer.inner"),
        ("m.outer.inner", "m.outer"),
        ("m.outer", "m.outer.inner"),
    }


def test_readonly_global_and_nonlocal():
    _, calls = graph(
        "def f(): pass\ndef outer():\n    def f(): pass\n"
        "    def inner():\n        nonlocal f\n        return f()\n"
        "    def global_call():\n        global f\n        return f()\n"
    )
    assert calls == {("m.outer.inner", "m.outer.f"), ("m.outer.global_call", "m.f")}


@pytest.mark.parametrize(
    "body",
    [
        "    def inner(f): return f()\n",
        "    def inner():\n        f = other\n        return f()\n",
        "    def inner():\n        nonlocal f\n        f = other\n        return f()\n",
        "    def inner():\n        del f\n        return f()\n",
        "    def inner():\n        with resource as f: f()\n",
        "    def inner():\n        for f in values: f()\n",
        "    def inner():\n        try: pass\n        except Exception as f: f()\n",
        "    def inner(value):\n        match value:\n            case f: f()\n",
    ],
)
def test_nested_shadowing_does_not_fall_through(body):
    result, calls = graph("def f(): pass\ndef outer():\n    def f(): pass\n" + body)
    assert not calls
    assert any(r.resolution_state == "unresolved" for r in result.references)


def test_global_rebinding_invalidates_other_readers():
    _, calls = graph(
        "def f(): pass\ndef replace():\n    global f\n    f = other\ndef reader(): return f()\n"
    )
    assert not calls


def test_nonlocal_rebinding_invalidates_sibling_readers():
    _, calls = graph(
        "def outer():\n    def f(): pass\n    def replace():\n"
        "        nonlocal f\n        f = other\n    def reader(): return f()\n"
    )
    assert not calls


def test_method_free_names_skip_class_namespace():
    _, calls = graph(
        "def f(): pass\nclass C:\n    def f(self): pass\n    def run(self): return f()\n"
    )
    assert calls == {("m.C.run", "m.f")}


def test_method_inside_nested_class_sees_enclosing_function():
    _, calls = graph(
        "def outer():\n    def f(): pass\n    class C:\n"
        "        def f(self): pass\n        def run(self): return f()\n"
    )
    assert calls == {("m.outer.C.run", "m.outer.f")}


def test_lambda_and_comprehension_calls_stay_unknown_without_scope_leak():
    _, calls = graph(
        "def f(): pass\ndef g():\n    xs = [f() for f in values]\n"
        "    callback = lambda f: f()\n    return f()\n"
    )
    assert calls == {("m.g", "m.f")}


def test_comprehension_walrus_rebinding_invalidates_enclosing_name():
    _, calls = graph(
        "def f(): pass\ndef g():\n    [(f := value) for value in values]\n    return f()\n"
    )
    assert not calls


def test_definition_time_calls_are_not_assigned_to_function_body():
    result, calls = graph("def f(): pass\ndef g(x=f(), *args: f(), **kwargs: f()) -> f(): pass\n")
    assert not calls
    assert len([r for r in result.references if r.syntax_kind == "call"]) == 4
    assert all(
        r.source_id == result.symbols[0].id for r in result.references if r.syntax_kind == "call"
    )


def test_invalid_nonlocal_and_return_are_diagnosed_without_execution():
    for source in ["def g():\n    nonlocal missing\n", "return 1\n"]:
        result, calls = graph(source)
        assert not calls
        assert result.skipped_files == 1
        assert result.diagnostics[0].code == "parse_error"


def test_conditional_nested_definition_does_not_resolve():
    _, calls = graph("def outer():\n    if flag:\n        def inner(): pass\n    inner()\n")
    assert not calls


def test_duplicate_nested_names_do_not_resolve():
    _, calls = graph("def outer():\n    def inner(): pass\n    def inner(): pass\n    inner()\n")
    assert not calls
