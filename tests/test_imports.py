from __future__ import annotations

import pytest

from ripple.config import SourceConfig
from ripple.frontend import build_snapshot
from ripple.git_source import SourceFile


def snapshot(files: dict[str, str], config: SourceConfig | None = None):
    return build_snapshot(
        "b" * 40, [SourceFile(path, data.encode()) for path, data in files.items()], [], config
    )


def calls(result):
    names = {s.id: s.display_name for s in result.symbols}
    return {
        (names[e.source_id], names[e.target_id]) for e in result.edges if e.edge_type == "CALLS"
    }


@pytest.mark.parametrize(
    "statement,expression",
    [
        ("from pkg.util import f as alias", "alias"),
        ("import pkg.util as mod", "mod.f"),
        ("import pkg.util", "pkg.util.f"),
        ("from .util import f", "f"),
        ("from . import util", "util.f"),
        ("from pkg import util", "util.f"),
    ],
)
def test_package_imports_and_aliases(statement, expression):
    result = snapshot(
        {
            "src/pkg/__init__.py": "",
            "src/pkg/util.py": "def f(): pass\n",
            "src/pkg/api.py": statement + "\ndef run(): return " + expression + "()\n",
        }
    )
    assert calls(result) == {("pkg.api.run", "pkg.util.f")}
    assert result.source_roots == ("src", ".")


def test_parent_relative_import_and_package_init():
    result = snapshot(
        {
            "pkg/__init__.py": "from .util import f\ndef run(): return f()\n",
            "pkg/util.py": "def f(): pass\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/api.py": "from ..util import f\ndef run(): return f()\n",
        }
    )
    assert calls(result) == {("pkg.run", "pkg.util.f"), ("pkg.sub.api.run", "pkg.util.f")}


def test_module_name_ending_in_init_is_not_a_package():
    result = snapshot(
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def f(): pass\n",
            "pkg/foo__init__.py": "from .util import f\ndef run(): return f()\n",
        }
    )
    assert calls(result) == {("pkg.foo__init__.run", "pkg.util.f")}


def test_function_local_import_is_visible_only_in_its_scope():
    result = snapshot(
        {
            "util.py": "def f(): pass\n",
            "api.py": "def a():\n    from util import f as alias\n    return alias()\n"
            "def b(): return alias()\n",
        }
    )
    assert calls(result) == {("api.a", "util.f")}
    imported = next(r for r in result.references if r.syntax_kind == "import")
    assert next(s for s in result.symbols if s.id == imported.source_id).display_name == "api.a"


@pytest.mark.parametrize(
    "source",
    [
        "from .util import f\ndef run(f): return f()\n",
        "import pkg.util as mod\ndef run(mod): return mod.f()\n",
        "import pkg.util as mod\nmod = other\ndef run(): return mod.f()\n",
        "if flag:\n    from .util import f\ndef run(): return f()\n",
        "try:\n    from .util import f\nexcept ImportError: pass\ndef run(): return f()\n",
        "from .util import *\ndef run(): return f()\n",
        "from ...util import f\ndef run(): return f()\n",
    ],
)
def test_ambiguous_or_shadowed_import_does_not_resolve(source):
    result = snapshot(
        {"pkg/__init__.py": "", "pkg/util.py": "def f(): pass\n", "pkg/api.py": source}
    )
    assert not calls(result)
    assert any(
        r.syntax_kind == "call" and r.resolution_state == "unresolved" for r in result.references
    )


def test_namespace_package_is_local():
    result = snapshot(
        {
            "src/pkg/util.py": "def f(): pass\n",
            "src/pkg/api.py": "from .util import f\ndef run(): f()\n",
        }
    )
    assert calls(result) == {("pkg.api.run", "pkg.util.f")}


def test_ambiguous_multiple_roots_do_not_choose_a_winner():
    result = snapshot(
        {
            "one/pkg/util.py": "def f(): pass\n",
            "two/pkg/util.py": "def f(): pass\n",
            "one/pkg/api.py": "from .util import f\ndef run(): f()\n",
        },
        SourceConfig(("one", "two")),
    )
    assert not calls(result)
    assert any(d.code == "ambiguous_module" for d in result.diagnostics)


def test_plain_module_cannot_be_a_parent_package():
    result = snapshot(
        {
            "pkg.py": "",
            "pkg/util.py": "def f(): pass\n",
            "api.py": "from pkg.util import f\ndef run(): f()\n",
        }
    )
    assert not calls(result)


def test_reexports_are_explicitly_unresolved():
    result = snapshot(
        {
            "pkg/__init__.py": "from .util import f\n",
            "pkg/util.py": "def f(): pass\n",
            "api.py": "from pkg import f\ndef run(): f()\n",
        }
    )
    assert not calls(result)
    assert any(r.resolver_rule == "python.reexport-unsupported-v2" for r in result.references)


def test_known_external_and_missing_modules_stay_distinct():
    result = snapshot(
        {
            "m.py": "import json\nimport nonexistent_package\ndef g():\n"
            "    json.dumps({})\n    nonexistent_package.send()\n    len([])\n"
        }
    )
    assert not calls(result)
    refs = {r.expression: r.resolution_state for r in result.references}
    assert refs["json"] == refs["json.dumps"] == refs["len"] == "external"
    assert refs["nonexistent_package"] == refs["nonexistent_package.send"] == "unresolved"


def test_local_stdlib_name_has_precedence_over_external_classification():
    result = snapshot(
        {"json.py": "def dumps(x): pass\n", "m.py": "import json\ndef g(): json.dumps({})\n"}
    )
    assert calls(result) == {("m.g", "json.dumps")}


def test_failed_local_stdlib_name_is_not_mislabeled_external():
    result = snapshot({"json.py": "def ???", "m.py": "import json\ndef g(): json.dumps({})\n"})
    assert not calls(result)
    assert all(r.resolution_state == "unresolved" for r in result.references)


def test_dynamic_import_does_not_infer_target_module():
    result = snapshot({"m.py": "import importlib\ndef g(name): importlib.import_module(name)\n"})
    assert not calls(result)
    ref = next(r for r in result.references if r.syntax_kind == "call")
    assert ref.resolution_state == "unresolved"
    assert ref.resolver_rule == "python.dynamic-import-v2"


def test_module_attribute_mutation_invalidates_alias_and_export_calls():
    result = snapshot(
        {
            "util.py": "def f(): pass\n",
            "a.py": "import util\nutil.f = other\n",
            "b.py": "from util import f\ndef g(): return f()\n",
        }
    )
    assert not calls(result)
    assert any(d.code == "attribute_mutation" for d in result.diagnostics)


def test_unimported_child_module_does_not_create_resolved_call():
    result = snapshot(
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def f(): pass\n",
            "api.py": "import pkg\ndef g(): return pkg.util.f()\n",
        }
    )
    assert not calls(result)


def test_missing_dotted_import_does_not_bind_other_children():
    result = snapshot(
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def f(): pass\n",
            "api.py": "import pkg.missing\ndef g(): return pkg.util.f()\n",
        }
    )
    assert not calls(result)


def test_aliased_dynamic_import_stays_unresolved():
    result = snapshot(
        {"m.py": "from importlib import import_module as load\ndef g(): load(name)\n"}
    )
    assert not calls(result)
    assert (
        next(r for r in result.references if r.syntax_kind == "call").resolution_state
        == "unresolved"
    )


def test_aliased_dynamic_namespace_access_blocks_function_resolution():
    result = snapshot(
        {
            "m.py": "from builtins import exec as execute\ndef f(): pass\n"
            "def g():\n    execute(source)\n    f()\n"
        }
    )
    assert not calls(result)
