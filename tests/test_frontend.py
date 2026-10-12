from __future__ import annotations

import pytest

from ripple.frontend import build_snapshot
from ripple.git_source import SourceFile


def snapshot(**files: str):
    return build_snapshot(
        "a" * 40,
        [SourceFile(p.replace("__", "/") + ".py", c.encode()) for p, c in files.items()],
        [],
    )


def calls(result):
    symbols = {s.id: s.display_name for s in result.symbols}
    return {
        (symbols[e.source_id], symbols[e.target_id]) for e in result.edges if e.edge_type == "CALLS"
    }


def test_same_file_and_alias_calls():
    result = snapshot(
        util="def f():\n    return 1\n",
        api="from util import f as alias\ndef wrapper():\n    return alias()\n"
        "async def endpoint():\n    return wrapper()\n",
    )
    assert calls(result) == {("api.wrapper", "util.f"), ("api.endpoint", "api.wrapper")}
    assert all(e.resolver_rule for e in result.edges)


@pytest.mark.parametrize(
    "body",
    [
        "def wrapper(f):\n    return f()\n",
        "def wrapper():\n    f = replacement\n    return f()\n",
        "def wrapper():\n    f()\n    f = replacement\n",
        "def wrapper():\n    try: pass\n    except Exception as f: f()\n",
        "def wrapper(value):\n    match value:\n        case f: return f()\n",
        "def wrapper():\n    return [f() for f in values]\n",
        "def wrapper():\n    return (lambda f: f())(value)\n",
    ],
)
def test_shadowing_never_invents_imported_call(body):
    result = snapshot(util="def f():\n    return 1\n", api="from util import f\n" + body)
    assert not calls(result)
    assert any(
        r.syntax_kind == "call" and r.resolution_state == "unresolved" for r in result.references
    )


@pytest.mark.parametrize(
    "source",
    [
        "def f(): pass\nf = replacement\ndef wrapper(): f()\n",
        "if flag:\n    def f(): pass\ndef wrapper(): f()\n",
        "def f(): pass\ndef f(): pass\ndef wrapper(): f()\n",
        "@replace\ndef f(): pass\ndef wrapper(): f()\n",
        "from util import *\ndef f(): pass\ndef wrapper(): f()\n",
        "def f(): pass\ndef wrapper():\n    exec(code)\n    f()\n",
    ],
)
def test_unsupported_patterns_are_not_resolved(source):
    assert not calls(snapshot(api=source))


def test_unknown_receiver_not_matched_by_method_name():
    result = snapshot(
        api="class Email:\n    def send(self): pass\n"
        "class SMS:\n    def send(self): pass\n"
        "def notify(service):\n    service.send()\n"
    )
    assert not calls(result)
    assert {s.qualname for s in result.symbols} >= {"Email.send", "SMS.send", "notify"}
    assert any(
        r.expression == "service.send" and r.resolution_state == "unresolved"
        for r in result.references
    )


def test_import_only_does_not_create_call():
    result = snapshot(util="def f(): pass\n", api="from util import f\n")
    assert not calls(result)
    assert {e.edge_type for e in result.edges} == {"IMPORTS_SYMBOL"}


def test_imported_target_dynamic_namespace_is_not_resolved():
    result = snapshot(
        util="from dynamic import *\ndef f(): pass\n",
        api="from util import f\ndef wrapper(): f()\n",
    )
    assert not calls(result)


def test_module_collision_prevents_arbitrary_target():
    result = build_snapshot(
        "a" * 40,
        [
            SourceFile("pkg.py", b"def f(): pass\n"),
            SourceFile("pkg/__init__.py", b"def f(): pass\n"),
            SourceFile("api.py", b"from pkg import f\ndef wrapper(): f()\n"),
        ],
        [],
    )
    assert not calls(result)
    assert any(d.code == "ambiguous_module" for d in result.diagnostics)


def test_encoding_crlf_unicode_and_exact_utf8_column():
    result = build_snapshot(
        "a" * 40,
        [
            SourceFile("latin.py", b"# coding: latin-1\r\ndef f(): return 'caf\xe9'\r\n"),
            SourceFile("unicode.py", "\ufeffdef f(): pass\ndef g(): \u03b1 = 0; f()\n".encode()),
        ],
        [],
    )
    assert result.parsed_files == 2
    call = next(r for r in result.references if r.syntax_kind == "call")
    assert call.location.line == 2
    assert call.location.column == len("def g(): \u03b1 = 0; ".encode("utf-8"))


def test_parse_failure_preserves_other_files():
    result = snapshot(broken="def ???", good="def f(): pass\ndef g(): f()\n")
    assert result.skipped_files == 1
    assert calls(result) == {("good.g", "good.f")}
    assert any(d.code == "parse_error" for d in result.diagnostics)


def test_ast_node_budget(monkeypatch):
    monkeypatch.setattr("ripple.frontend.MAX_AST_NODES", 3)
    result = snapshot(big="def f(): pass\n")
    assert result.skipped_files == 1
    assert result.diagnostics[0].code == "ast_node_limit"
