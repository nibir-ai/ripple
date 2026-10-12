"""Conservative lexical and repository-local resolution of extracted syntax sites."""

from __future__ import annotations

import ast
import builtins
import sys
from dataclasses import dataclass, replace

from ripple.models import Diagnostic, Edge, Reference, State, Symbol, stable_id
from ripple.python_extract import (
    Binding,
    Definition,
    ImportBinding,
    ModuleFacts,
    Scope,
    Site,
    location,
)


@dataclass(frozen=True)
class Target:
    state: State
    rule: str
    reason: str
    symbol: Symbol | None = None
    module: str | None = None
    external: str | None = None
    definition: Definition | None = None
    receiver: bool = False
    module_limit: str | None = None


def unknown(reason: str, rule: str = "python.unresolved-call-v1") -> Target:
    return Target("unresolved", rule, reason)


class Resolver:
    def __init__(self, modules: dict[str, list[ModuleFacts]], unavailable: set[str]):
        self.modules = modules
        self.unavailable = unavailable
        self.by_scope = {s: f for group in modules.values() for f in group for s in f.scopes}
        self.definitions = {
            d.symbol.id: d for group in modules.values() for f in group for d in f.definitions
        }
        self.invalidated: set[tuple[str, str]] = set()

    def local_module(self, module: str) -> Target:
        parts = module.split(".")
        for end in range(1, len(parts)):
            prefix = ".".join(parts[:end])
            ancestors = self.modules.get(prefix, [])
            if len(ancestors) > 1 or (len(ancestors) == 1 and not ancestors[0].is_package):
                return unknown("Parent package mapping is ambiguous or is an ordinary module.")
        group = self.modules.get(module, [])
        if len(group) > 1:
            return unknown(
                "Multiple source files map to this module.", "python.ambiguous-module-v1"
            )
        if len(group) == 1:
            return Target(
                "resolved",
                "python.module-import-v2",
                "Unique repository module.",
                group[0].module_scope.owner,
                module=module,
                module_limit=module,
            )
        if module in self.unavailable:
            return unknown("Local module was excluded or could not be parsed.")
        if any(name.startswith(module + ".") for name in self.modules):
            return Target(
                "resolved",
                "python.namespace-package-v2",
                "Repository namespace prefix.",
                module=module,
                module_limit=module,
            )
        root = module.split(".")[0]
        local_root = any(name == root or name.startswith(root + ".") for name in self.modules)
        blocked_root = any(name == root or name.startswith(root + ".") for name in self.unavailable)
        if root in sys.stdlib_module_names and not local_root and not blocked_root:
            return Target(
                "external",
                "python.stdlib-import-v2",
                "Known standard-library module; external code is not traversed.",
                external=module,
            )
        return unknown(
            "Module missing from configured source scope; environment is not inferred.",
            "python.missing-module-v2",
        )

    def relative_module(self, imported: ImportBinding, facts: ModuleFacts) -> str | None:
        if imported.level == 0:
            return imported.module
        package = facts.module if facts.is_package else facts.module.rpartition(".")[0]
        parts = package.split(".") if package and package != "__root__" else []
        if imported.level > len(parts):
            return None
        prefix = parts[: len(parts) - imported.level + 1]
        return ".".join([*prefix, *([imported.module] if imported.module else [])])

    def imported(self, binding: ImportBinding, scope: Scope, expression: bool = True) -> Target:
        if not binding.active or binding.name == "*":
            return unknown(
                "Conditional or wildcard import has no unique supported binding.",
                "python.unsupported-import-v1",
            )
        module = self.relative_module(binding, self.by_scope[scope])
        if module is None:
            return unknown(
                "Relative import escapes the mapped package.", "python.relative-import-v2"
            )
        if binding.name is None:
            full_target = self.local_module(module)
            if full_target.state != "resolved" or not expression:
                return full_target
            if expression and isinstance(binding.node, ast.Import):
                alias = next(a for a in binding.node.names if a.name == binding.module)
                if alias.asname is None:
                    return replace(self.local_module(module.split(".")[0]), module_limit=module)
            return full_target
        parent = self.local_module(module)
        if parent.state == "external":
            return Target(
                "external",
                "python.external-from-import-v2",
                parent.reason,
                external=module + "." + binding.name,
            )
        if parent.state != "resolved":
            return parent
        return self.member(parent, binding.name, importing=True)

    def binding(self, binding: Binding, scope: Scope, name: str) -> Target:
        if binding.symbol is not None:
            symbol = binding.symbol
            if not symbol.supported:
                return unknown("Definition is conditional, decorated or unsupported.")
            return Target(
                "resolved",
                "python.lexical-function-v2",
                "Unique lexical definition; runtime mutation remains possible.",
                symbol,
                definition=self.definitions[symbol.id],
            )
        if binding.imported is not None:
            return self.imported(binding.imported, scope)
        if binding.kind == "parameter" and name == scope.receiver and scope.parent is not None:
            parent = self.definitions.get(scope.parent.owner.id)
            method = self.definitions.get(scope.owner.id)
            if parent is not None and method is not None and self.builtin_decorator(method):
                return Target(
                    "resolved",
                    "python.lexical-receiver-v2",
                    "Lexical method receiver.",
                    parent.symbol,
                    definition=parent,
                    receiver=True,
                )
        return unknown(
            "Name is parameter-bound, assigned, deleted or otherwise rebound; "
            "no callable value is inferred.",
            "python.shadowed-name-v2",
        )

    def name(self, scope: Scope, name: str) -> Target:
        original = scope
        while True:
            if scope.dynamic:
                return unknown("Dynamic namespace access prevents unique binding resolution.")
            if len(self.modules[self.by_scope[scope].module]) > 1:
                return unknown("Module mapping is ambiguous.", "python.ambiguous-module-v1")
            if name in scope.globals:
                if scope.kind != "module":
                    scope = scope.module_scope()
                    continue
            elif name in scope.nonlocals:
                destination = scope.nonlocal_scope(name)
                if destination is None:
                    return unknown("Nonlocal binding cannot be established.")
                scope = destination
                continue
            if scope.kind == "module" and (self.by_scope[scope].module, name) in self.invalidated:
                return unknown("Explicit module attribute write invalidates this binding.")
            values = scope.bindings.get(name, [])
            if values:
                if len(values) != 1:
                    return unknown(
                        "Multiple definitions or rebindings prevent unique resolution.",
                        "python.ambiguous-binding-v2",
                    )
                return self.binding(values[0], scope, name)
            parent = scope.parent
            while parent is not None and parent.kind == "class":
                parent = parent.parent
            if parent is None:
                break
            scope = parent
        if name in vars(builtins):
            if name in {"exec", "eval", "globals", "locals", "setattr", "delattr", "__import__"}:
                return unknown(
                    "Dynamic builtin semantics are not inferred.", "python.dynamic-call-v2"
                )
            return Target(
                "external",
                "python.builtin-call-v2",
                "Unshadowed builtin; external implementation is not traversed.",
                external="builtins." + name,
            )
        return unknown(
            f"No unique binding for {name!r} in lexical scope {original.owner.qualname}."
        )

    def builtin_decorator(self, definition: Definition) -> bool:
        decorators = definition.node.decorator_list
        if not decorators:
            return True
        if len(decorators) != 1 or not isinstance(decorators[0], ast.Name):
            return False
        name = decorators[0].id
        return (
            name in {"staticmethod", "classmethod"}
            and self.name(definition.enclosing, name).external == "builtins." + name
        )

    def member(self, parent: Target, name: str, importing: bool = False) -> Target:
        if parent.external is not None:
            qualified = parent.external + "." + name
            if qualified in {"importlib.import_module", "builtins.__import__"}:
                return unknown("Dynamic import target is not inferred.", "python.dynamic-import-v2")
            return Target(
                "external", "python.external-member-v2", parent.reason, external=qualified
            )
        if parent.module is not None:
            key = parent.module
            if (key, name) in self.invalidated:
                return unknown("Explicit module attribute write invalidates this binding.")
            group = self.modules.get(key, [])
            if len(group) == 1:
                scope = group[0].module_scope
                if scope.dynamic:
                    return unknown("Exporting module has dynamic namespace bindings.")
                values = scope.bindings.get(name, [])
                if len(values) == 1 and values[0].symbol is not None:
                    result = self.binding(values[0], scope, name)
                    return Target(
                        result.state,
                        "python.imported-function-call-v1",
                        result.reason,
                        result.symbol,
                        definition=result.definition,
                    )
                if values:
                    return unknown(
                        "Re-exported or rebound module member is not inferred.",
                        "python.reexport-unsupported-v2",
                    )
            child = key + "." + name
            if child in self.modules or any(m.startswith(child + ".") for m in self.modules):
                if importing or (
                    parent.module_limit is not None
                    and (
                        parent.module_limit == child or parent.module_limit.startswith(child + ".")
                    )
                ):
                    return replace(
                        self.local_module(child), module_limit=parent.module_limit or child
                    )
                return unknown("Submodule exists but is not bound by the observed import.")
            return unknown("Requested member is not a uniquely supported repository definition.")
        definition = parent.definition
        if definition is not None and isinstance(definition.node, ast.ClassDef):
            if len(definition.enclosing.bindings.get(definition.node.name, [])) != 1:
                return unknown("Lexical class name is duplicated or rebound.")
            if parent.receiver and (
                {"__getattribute__", "__getattr__"} & definition.scope.bindings.keys()
            ):
                return unknown(
                    "Custom attribute hooks prevent lexical receiver dispatch.",
                    "python.method-dispatch-unknown-v2",
                )
            if (definition.symbol.id, name) in self.invalidated:
                return unknown(
                    "Explicit receiver/class attribute write invalidates method binding."
                )
            node = definition.node
            if (
                node.bases
                or node.keywords
                or node.decorator_list
                or not definition.symbol.supported
            ):
                return unknown(
                    "Inheritance, metaclasses or class decorators prevent method resolution.",
                    "python.method-dispatch-unknown-v2",
                )
            values = definition.scope.bindings.get(name, [])
            if len(values) == 1 and values[0].symbol is not None:
                target = self.definitions[values[0].symbol.id]
                if (
                    target.symbol.kind in {"method", "async_method"}
                    and target.symbol.supported
                    and self.builtin_decorator(target)
                    and not definition.scope.dynamic
                ):
                    return Target(
                        "resolved",
                        "python.lexical-method-v2",
                        "Unique lexical method; "
                        "runtime subclass overrides, descriptors and mutation may change dispatch.",
                        target.symbol,
                        definition=target,
                    )
            return unknown("Method is missing, shadowed, duplicated or decorated.")
        return unknown("Unknown object receiver; method name alone does not establish a target.")

    def expression(self, scope: Scope, node: ast.expr) -> Target:
        if isinstance(node, ast.Name):
            return self.name(scope, node.id)
        if isinstance(node, ast.Attribute):
            parent = self.expression(scope, node.value)
            if parent.state != "resolved" and parent.external is None:
                return parent
            return self.member(parent, node.attr)
        return unknown("Dynamic call expression, subscript, factory or unknown receiver.")

    def invalidate_writes(self) -> list[Diagnostic]:
        diagnostics = []
        for group in self.modules.values():
            for facts in group:
                for site in facts.sites:
                    if site.kind == "call" and not site.opaque:
                        target = self.expression(site.scope, site.expression)
                        if target.external in {
                            "builtins.exec",
                            "builtins.eval",
                            "builtins.globals",
                            "builtins.locals",
                            "builtins.setattr",
                            "builtins.delattr",
                        }:
                            site.scope.dynamic = True
        for group in self.modules.values():
            for facts in group:
                for scope, node in facts.attribute_writes:
                    parent = self.expression(scope, node.value)
                    key = parent.module or (
                        parent.symbol.id
                        if parent.symbol is not None and parent.definition is not None
                        else None
                    )
                    if key is not None:
                        self.invalidated.add((key, node.attr))
                    diagnostics.append(
                        Diagnostic(
                            "attribute_mutation",
                            "Attribute write recorded; "
                            "matching member claims are invalidated when the receiver is known.",
                            facts.path,
                            node.lineno,
                        )
                    )
        return diagnostics

    def site_target(self, site: Site) -> Target:
        if site.kind == "decorator" and not isinstance(site.expression, ast.Call):
            target = self.expression(site.scope, site.expression)
            return Target(
                target.state,
                target.rule,
                target.reason + " Decorator reference may "
                "replace callable behavior; it is not a caller relationship.",
                target.symbol,
                definition=target.definition,
                external=target.external,
            )
        if site.opaque or site.scope.kind in {"opaque", "comprehension"}:
            return unknown(
                "Decorator/default/annotation/lambda/comprehension semantics are not "
                "treated as body call evidence.",
                "python.scoped-expression-unknown-v2",
            )
        target = self.expression(site.scope, site.expression)
        if site.kind == "call" and target.external in {
            "importlib.import_module",
            "builtins.__import__",
            "builtins.exec",
            "builtins.eval",
            "builtins.globals",
            "builtins.locals",
            "builtins.setattr",
            "builtins.delattr",
        }:
            return unknown(
                "Aliased dynamic builtin/import operation has no inferred target.",
                "python.dynamic-call-v2",
            )
        if site.kind == "call" and target.state == "resolved":
            if target.symbol is None or target.symbol.kind == "module":
                return unknown("Module/namespace bindings are not callable targets.")
            if target.receiver:
                return unknown("Receiver value is not inferred to be callable.")
            definition = target.definition
            if definition is not None and not self.builtin_decorator(definition):
                return unknown("Decorator binding is shadowed or unsupported.")
            if target.symbol.kind == "class":
                return Target(
                    "resolved",
                    "python.class-constructor-v2",
                    "Call to a lexical class; "
                    "__new__/__init__/metaclass side effects are not expanded.",
                    target.symbol,
                    definition=definition,
                )
            if (
                isinstance(site.expression, ast.Name)
                and target.symbol.module == self.by_scope[site.scope].module
                and "." not in target.symbol.qualname
            ):
                return Target(
                    "resolved",
                    "python.same-module-function-v1",
                    target.reason,
                    target.symbol,
                    definition=definition,
                )
        return target


def resolve(
    modules: dict[str, list[ModuleFacts]], unavailable: set[str], namespace: str
) -> tuple[list[Symbol], list[Reference], list[Edge], list[Diagnostic]]:
    resolver = Resolver(modules, unavailable)
    diagnostics = resolver.invalidate_writes()
    symbols: list[Symbol] = []
    references: list[Reference] = []
    edges: list[Edge] = []

    def emit(
        facts: ModuleFacts,
        scope: Scope,
        node: ast.AST,
        kind: str,
        expression: str,
        target: Target,
        ordinal: int,
        source_symbol: Symbol | None = None,
    ) -> None:
        site = location(facts.path, node)
        reference = Reference(
            stable_id(namespace, facts.path, kind, str(site.line), str(site.column), str(ordinal)),
            (source_symbol or scope.owner).id,
            kind,
            expression[:200],
            site,
            target.state,
            target.rule,
            target.reason,
            target.symbol.id if target.symbol else None,
        )
        references.append(reference)
        edge_type = {
            "call": "CALLS",
            "import": "IMPORTS_SYMBOL",
            "base_class": "INHERITS",
            "decorator": "DECORATES",
        }.get(kind)
        if kind == "import" and target.module:
            edge_type = "IMPORTS_MODULE"
        if target.state == "resolved" and target.symbol is not None and edge_type:
            edges.append(
                Edge(
                    (source_symbol or scope.owner).id,
                    target.symbol.id,
                    edge_type,
                    site,
                    target.rule,
                    target.reason,
                )
            )
        if target.state == "unresolved":
            diagnostics.append(
                Diagnostic("unresolved_" + kind, target.reason, facts.path, site.line)
            )

    for module in sorted(modules):
        for facts in modules[module]:
            symbols.extend([facts.module_scope.owner, *(d.symbol for d in facts.definitions)])
            if len(modules[module]) > 1:
                diagnostics.append(
                    Diagnostic("ambiguous_module", "Multiple files map to module.", facts.path)
                )
            for definition in facts.definitions:
                if not definition.symbol.supported or not resolver.builtin_decorator(definition):
                    diagnostics.append(
                        Diagnostic(
                            "unsupported_definition",
                            "Conditional, generic "
                            "or custom-decorated definition is not a resolved target.",
                            facts.path,
                            definition.symbol.location.line,
                        )
                    )
            for scope in facts.scopes:
                if scope.dynamic:
                    diagnostics.append(
                        Diagnostic(
                            "dynamic_binding",
                            "Wildcard import or dynamic "
                            "namespace operation blocks unique resolution.",
                            facts.path,
                            scope.owner.location.line,
                        )
                    )
            for ordinal, (scope, imported) in enumerate(facts.imports):
                target = resolver.imported(imported, scope, expression=False)
                if target.state == "resolved" and target.symbol and not target.module:
                    target = Target(
                        "resolved",
                        "python.direct-from-import-v1",
                        "Import-only evidence does not propagate caller impact.",
                        target.symbol,
                        definition=target.definition,
                    )
                emit(facts, scope, imported.node, "import", imported.expression, target, ordinal)
            for ordinal, site in enumerate(facts.sites):
                target = resolver.site_target(site)
                if site.kind == "decorator" and isinstance(site.expression, ast.Name):
                    if site.expression.id in {"staticmethod", "classmethod"}:
                        target = resolver.name(site.scope, site.expression.id)
                emit(
                    facts,
                    site.scope,
                    site.node,
                    site.kind,
                    ast.unparse(site.expression),
                    target,
                    ordinal,
                    site.source_symbol,
                )
    return symbols, references, edges, diagnostics
