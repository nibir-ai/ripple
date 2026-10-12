from __future__ import annotations

import pytest

from ripple.frontend import build_snapshot
from ripple.git_source import SourceFile


def graph(source):
    result = build_snapshot("a" * 40, [SourceFile("m.py", source.encode())], [])
    names = {s.id: s.display_name for s in result.symbols}
    return result, {
        (names[e.source_id], names[e.target_id]) for e in result.edges if e.edge_type == "CALLS"
    }


def test_self_method_is_lexical_with_dispatch_caveat():
    result, calls = graph("class C:\n    def f(self): pass\n    def g(self): return self.f()\n")
    assert calls == {("m.C.g", "m.C.f")}
    edge = next(e for e in result.edges if e.edge_type == "CALLS")
    assert edge.resolver_rule == "python.lexical-method-v2"
    assert "runtime subclass overrides" in edge.explanation


def test_class_name_static_and_cls_method_resolution():
    _, calls = graph(
        "class C:\n    @staticmethod\n    def f(): pass\n"
        "    @classmethod\n    def g(cls): return cls.f()\n"
        "def caller(): C.g()\n"
    )
    assert calls == {("m.C.g", "m.C.f"), ("m.caller", "m.C.g")}


def test_async_method_and_constructor_class_target():
    result, calls = graph(
        "class C:\n    async def f(self): pass\n"
        "    async def g(self): return await self.f()\ndef caller(): return C()\n"
    )
    assert calls == {("m.C.g", "m.C.f"), ("m.caller", "m.C")}
    assert next(s for s in result.symbols if s.qualname == "C.f").kind == "async_method"


@pytest.mark.parametrize(
    "source",
    [
        "class C:\n    def f(self): pass\ndef g(obj): obj.f()\n",
        "class C:\n    def f(self): pass\n    def g(self):\n"
        "        self = other\n        self.f()\n",
        "class C(Base):\n    def f(self): pass\n    def g(self): self.f()\n",
        "class C:\n    @property\n    def f(self): pass\n    def g(self): self.f()\n",
        "class C:\n    @wrap\n    def f(self): pass\n    def g(self): self.f()\n",
        "class C:\n    def f(self): pass\n    f = other\n    def g(self): self.f()\n",
        "class C:\n    def f(self): pass\n    def f(self): pass\n    def g(self): self.f()\n",
        "class C:\n    def f(self): pass\n    def g(self): super().f()\n",
        "class C:\n    def f(self): pass\n    @staticmethod\n    def g(self): self.f()\n",
        "class C:\n    def f(self): pass\n    def g(self): self.f = other; self.f()\n",
        "class C:\n    def f(self): pass\nC.f = other\ndef g(): C.f()\n",
        "class C:\n    def f(self): pass\ndef g(C): C.f()\n",
        "@wrap\nclass C:\n    def f(self): pass\n    def g(self): self.f()\n",
        "classmethod = other\nclass C:\n    @classmethod\n    def f(cls): pass\ndef g(): C.f()\n",
    ],
)
def test_unknown_or_conflicting_method_dispatch_is_not_resolved(source):
    _, calls = graph(source)
    assert not calls


def test_known_inheritance_reference_does_not_propagate_as_call():
    result, calls = graph("class A: pass\nclass B(A): pass\n")
    assert not calls
    names = {s.id: s.display_name for s in result.symbols}
    assert {(names[e.source_id], names[e.target_id]) for e in result.edges} == {("m.B", "m.A")}


def test_source_span_includes_decorator_and_records_reference():
    result, _ = graph("class C:\n    @staticmethod\n    def f(): pass\n")
    symbol = next(s for s in result.symbols if s.qualname == "C.f")
    assert (symbol.location.line, symbol.location.column, symbol.location.end_line) == (2, 4, 3)
    assert any(r.syntax_kind == "decorator" and r.location.line == 2 for r in result.references)


def test_shadowed_classmethod_cannot_infer_cls_receiver():
    _, calls = graph(
        "classmethod = wrapper\nclass C:\n    def f(self): pass\n"
        "    @classmethod\n    def g(cls): return cls.f()\n"
    )
    assert not calls


def test_known_decorator_reference_has_definition_as_source():
    result, calls = graph("def wrap(fn): return fn\n@wrap\ndef f(): pass\n")
    assert not calls
    names = {s.id: s.display_name for s in result.symbols}
    assert {(names[e.source_id], names[e.target_id], e.edge_type) for e in result.edges} == {
        ("m.f", "m.wrap", "DECORATES")
    }


def test_multiline_decorator_span_starts_at_at_sign():
    result, _ = graph("@(\n    wrap\n)\ndef f(): pass\n")
    symbol = next(s for s in result.symbols if s.qualname == "f")
    assert (symbol.location.line, symbol.location.column) == (1, 0)


def test_custom_attribute_hook_does_not_infer_receiver_method():
    _, calls = graph(
        "class C:\n    def __getattribute__(self, name): return value\n"
        "    def f(self): pass\n    def g(self): self.f()\n"
    )
    assert not calls


def test_duplicate_class_names_do_not_infer_receiver_method():
    _, calls = graph("class C: pass\nclass C:\n    def f(self): pass\n    def g(self): self.f()\n")
    assert not calls
