"""AST syntax and lexical scope extraction, without target execution or resolution."""

from __future__ import annotations

import ast
import copy
import io
import tokenize
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from ripple.models import Location, Symbol, stable_id

Function = ast.FunctionDef | ast.AsyncFunctionDef


def digest(nodes: Sequence[ast.AST] | ast.AST) -> str:
    parts = [nodes] if isinstance(nodes, ast.AST) else nodes
    return stable_id(*(ast.dump(n, include_attributes=False) for n in parts))


def docstring_digest(body: list[ast.stmt]) -> str:
    return digest(body[:1] if body and is_docstring(body[0]) else [])


def is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


class DefinitionHeaders(ast.NodeTransformer):
    """Nested bodies belong to their own symbols; retain binding and header changes."""

    def generic_visit(self, node: ast.AST) -> ast.AST:
        node = copy.copy(node)
        for name, value in ast.iter_fields(node):
            if isinstance(value, list):
                setattr(node, name, list(value))
        return super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node = copy.copy(node)
        node.body = [ast.Pass()]
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        node = copy.copy(node)
        node.body = [ast.Pass()]
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node = copy.copy(node)
        node.body = [ast.Pass()]
        return node


def body_digest(body: list[ast.stmt]) -> str:
    runtime = body[1:] if body and is_docstring(body[0]) else body
    headers = DefinitionHeaders()
    return digest([headers.visit(node) for node in runtime])


def location(path: str, node: ast.AST) -> Location:
    return Location(
        path,
        getattr(node, "lineno", 1),
        getattr(node, "col_offset", 0),
        getattr(node, "end_lineno", 1) or 1,
        getattr(node, "end_col_offset", 0) or 0,
    )


@dataclass(frozen=True)
class ImportBinding:
    module: str
    name: str | None
    level: int
    expression: str
    node: ast.Import | ast.ImportFrom
    active: bool


@dataclass(frozen=True)
class Binding:
    kind: str
    symbol: Symbol | None = None
    imported: ImportBinding | None = None


@dataclass(eq=False)
class Scope:
    owner: Symbol
    kind: str
    parent: Scope | None
    bindings: dict[str, list[Binding]] = field(default_factory=lambda: defaultdict(list))
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)
    dynamic: bool = False
    receiver: str | None = None
    receiver_kind: str | None = None

    def bind(self, name: str, binding: Binding) -> None:
        self.bindings[name].append(binding)

    def module_scope(self) -> Scope:
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def nonlocal_scope(self, name: str) -> Scope | None:
        scope = self.parent
        while scope:
            if scope.kind == "function" and name in scope.bindings:
                return scope
            scope = scope.parent
        return None


@dataclass(frozen=True)
class Site:
    scope: Scope
    node: ast.AST
    expression: ast.expr
    kind: str
    opaque: bool = False
    source_symbol: Symbol | None = None


@dataclass
class Definition:
    symbol: Symbol
    node: Function | ast.ClassDef
    scope: Scope
    enclosing: Scope


@dataclass
class ModuleFacts:
    path: str
    module: str
    is_package: bool
    module_scope: Scope
    scopes: list[Scope] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    sites: list[Site] = field(default_factory=list)
    imports: list[tuple[Scope, ImportBinding]] = field(default_factory=list)
    attribute_writes: list[tuple[Scope, ast.Attribute]] = field(default_factory=list)


class Extractor(ast.NodeVisitor):
    def __init__(self, facts: ModuleFacts, namespace: str, source: str):
        self.facts = facts
        self.namespace = namespace
        self.scope = facts.module_scope
        self.direct = True
        self.opaque = False
        self.occurrences: dict[str, int] = defaultdict(int)
        lines = source.splitlines()
        self.decorator_starts = [
            (token.start[0], len(lines[token.start[0] - 1][: token.start[1]].encode("utf-8")))
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.OP
            and token.string == "@"
            and not lines[token.start[0] - 1][: token.start[1]].strip()
        ]

    def generic_visit(self, node: ast.AST) -> None:
        direct = self.direct
        if isinstance(node, ast.stmt):
            self.direct = False
        super().generic_visit(node)
        self.direct = direct

    def symbol(self, node: Function | ast.ClassDef) -> Symbol:
        parent = self.scope.owner
        qualname = node.name if parent.kind == "module" else parent.qualname + "." + node.name
        self.occurrences[qualname] += 1
        logical = self.facts.path + ":" + qualname
        if isinstance(node, ast.ClassDef):
            kind = "class"
            signature: list[ast.AST] = [*node.bases, *node.keywords]
        else:
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            if self.scope.kind == "class":
                kind = "async_method" if isinstance(node, ast.AsyncFunctionDef) else "method"
            signature = [node.args]
            if node.returns:
                signature.append(node.returns)
        signature.extend(getattr(node, "type_params", []))
        span = location(self.facts.path, node)
        if node.decorator_list:
            first = node.decorator_list[0]
            starts = [
                (line, column)
                for line, column in self.decorator_starts
                if line <= first.lineno and column == node.col_offset
            ]
            line, column = max(starts) if starts else (first.lineno, first.col_offset)
            span = Location(span.path, line, column, span.end_line, span.end_column)
        builtin_method = (
            self.scope.kind == "class"
            and len(node.decorator_list) == 1
            and isinstance(node.decorator_list[0], ast.Name)
            and node.decorator_list[0].id in {"staticmethod", "classmethod"}
        )
        return Symbol(
            stable_id(self.namespace, logical, str(self.occurrences[qualname])),
            logical,
            self.facts.module + "." + qualname,
            self.facts.module,
            qualname,
            kind,
            span,
            body_digest(node.body),
            digest(signature),
            digest(node.decorator_list),
            self.direct
            and (not node.decorator_list or builtin_method)
            and not getattr(node, "type_params", []),
            docstring_digest(node.body),
        )

    def headers(self, node: Function | ast.ClassDef, symbol: Symbol) -> None:
        old_opaque = self.opaque
        self.opaque = True
        for decorator in node.decorator_list:
            self.facts.sites.append(
                Site(self.scope, decorator, decorator, "decorator", True, symbol)
            )
            self.visit(decorator)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                self.facts.sites.append(Site(self.scope, base, base, "base_class", False, symbol))
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
        else:
            args = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                node.args.vararg,
                node.args.kwarg,
            ]
            for annotation in [node.returns, *(a.annotation for a in args if a is not None)]:
                if annotation is not None:
                    self.facts.sites.append(
                        Site(self.scope, annotation, annotation, "annotation", True, symbol)
                    )
                    self.visit(annotation)
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
        for param in getattr(node, "type_params", []):
            self.visit(param)
        self.opaque = old_opaque

    def definition(self, node: Function | ast.ClassDef) -> None:
        symbol = self.symbol(node)
        self.scope.bind(node.name, Binding("definition", symbol))
        self.headers(node, symbol)
        previous = self.scope, self.direct, self.opaque
        scope = Scope(symbol, "class" if isinstance(node, ast.ClassDef) else "function", self.scope)
        self.facts.scopes.append(scope)
        self.facts.definitions.append(Definition(symbol, node, scope, self.scope))
        if not isinstance(node, ast.ClassDef):
            for arg in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                node.args.vararg,
                node.args.kwarg,
            ]:
                if arg:
                    scope.bind(arg.arg, Binding("parameter"))
            positional = [*node.args.posonlyargs, *node.args.args]
            if self.scope.kind == "class" and positional:
                scope.receiver = positional[0].arg
                scope.receiver_kind = "self"
                if node.decorator_list:
                    deco = node.decorator_list[0]
                    if isinstance(deco, ast.Name) and deco.id == "classmethod":
                        scope.receiver_kind = "cls"
                    else:
                        scope.receiver = None
        self.scope, self.direct, self.opaque = scope, True, False
        for stmt in node.body:
            self.visit(stmt)
        self.scope, self.direct, self.opaque = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.definition(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            binding = ImportBinding(alias.name, None, 0, alias.name, node, self.direct)
            self.scope.bind(
                alias.asname or alias.name.split(".")[0], Binding("import", imported=binding)
            )
            self.facts.imports.append((self.scope, binding))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            binding = ImportBinding(
                node.module or "",
                alias.name,
                node.level,
                "." * node.level + (node.module or "") + "." + alias.name,
                node,
                self.direct,
            )
            self.scope.bind(alias.asname or alias.name, Binding("import", imported=binding))
            self.facts.imports.append((self.scope, binding))
            if alias.name == "*":
                self.scope.dynamic = True

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.bind(node.id, Binding("assignment"))

    def visit_Return(self, node: ast.Return) -> None:
        if self.scope.kind in {"module", "class"}:
            raise SyntaxError("Return outside function", (self.facts.path, node.lineno, 0, ""))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.facts.attribute_writes.append((self.scope, node))
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.nonlocals.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.scope.bind(node.name, Binding("exception"))
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.scope.bind(node.name, Binding("pattern"))
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.scope.bind(node.name, Binding("pattern"))

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.scope.bind(node.rest, Binding("pattern"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.facts.sites.append(Site(self.scope, node, node.func, "call", self.opaque))
        if isinstance(node.func, ast.Name) and node.func.id in {
            "exec",
            "eval",
            "globals",
            "locals",
            "setattr",
            "delattr",
        }:
            self.scope.dynamic = True
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default:
                self.visit(default)
        previous = self.scope, self.opaque
        self.scope = Scope(self.scope.owner, "opaque", self.scope)
        self.facts.scopes.append(self.scope)
        self.opaque = True
        self.visit(node.body)
        self.scope, self.opaque = previous

    def comprehension(self, node: ast.AST) -> None:
        previous = self.scope, self.opaque
        self.scope = Scope(self.scope.owner, "comprehension", self.scope)
        self.facts.scopes.append(self.scope)
        self.opaque = True
        self.generic_visit(node)
        self.scope, self.opaque = previous

    visit_ListComp = comprehension
    visit_SetComp = comprehension
    visit_DictComp = comprehension
    visit_GeneratorExp = comprehension

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        scope = self.scope
        while scope.kind == "comprehension" and scope.parent is not None:
            scope = scope.parent
        if isinstance(node.target, ast.Name):
            scope.bind(node.target.id, Binding("assignment"))
        self.visit(node.value)

    def visit_TypeAlias(self, node: ast.AST) -> None:
        # Type aliases and annotation scopes need a dedicated version-aware resolver.
        old = self.opaque
        self.opaque = True
        self.scope.dynamic = True
        self.generic_visit(node)
        self.opaque = old


def extract(path: str, module: str, tree: ast.Module, source: str, namespace: str) -> ModuleFacts:
    logical = path + ":<module>"
    lines = source.splitlines()
    owner = Symbol(
        stable_id(namespace, logical),
        logical,
        module,
        module,
        "<module>",
        "module",
        Location(path, 1, 0, max(1, len(lines)), len(lines[-1].encode("utf-8")) if lines else 0),
        body_digest(tree.body),
        digest([n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]),
        digest([]),
        True,
        docstring_digest(tree.body),
    )
    scope = Scope(owner, "module", None)
    facts = ModuleFacts(path, module, path.rsplit("/", 1)[-1] == "__init__.py", scope, [scope])
    visitor = Extractor(facts, namespace, source)
    for stmt in tree.body:
        visitor.visit(stmt)
    # Redirect explicit writes through global/nonlocal to invalidate the destination.
    for local in facts.scopes:
        for name in local.globals | local.nonlocals:
            if name in local.bindings:
                target = (
                    local.module_scope() if name in local.globals else local.nonlocal_scope(name)
                )
                if target is not None:
                    target.bind(name, Binding("redirected_write"))
                del local.bindings[name]
    return facts
