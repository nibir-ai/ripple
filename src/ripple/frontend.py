"""Parse immutable Python sources, then resolve repository-local lexical evidence."""

from __future__ import annotations

import ast
import io
import symtable
import sys
import tokenize
from collections import defaultdict

from ripple.config import SourceConfig
from ripple.git_source import SourceFile
from ripple.models import Diagnostic, Snapshot, stable_id
from ripple.python_extract import ModuleFacts, extract
from ripple.python_resolve import resolve

MAX_AST_NODES = 100000


def build_snapshot(
    tree: str,
    files: list[SourceFile],
    excluded: list[Diagnostic],
    config: SourceConfig | None = None,
    syntax: dict[str, ast.Module] | None = None,
) -> Snapshot:
    if config is None:
        config = SourceConfig(
            ("src", ".") if any(f.path.startswith("src/") for f in files) else (".",)
        )
    if syntax is None:
        syntax = {}
    namespace = stable_id(
        tree,
        config.fingerprint,
        "python-static-v1",
        f"{sys.version_info.major}.{sys.version_info.minor}",
    )
    modules: dict[str, list[ModuleFacts]] = defaultdict(list)
    diagnostics = list(excluded)
    skipped = len(excluded)
    unavailable = {
        module
        for d in excluded
        if d.path.endswith(".py") and (module := config.module_for(d.path)) is not None
    }
    for file in files:
        module = config.module_for(file.path)
        if module is None:
            diagnostics.append(
                Diagnostic("source_root_exclusion", "Outside configured source roots.", file.path)
            )
            skipped += 1
            continue
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(file.contents).readline)
            source = file.contents.decode(encoding)
            parsed = syntax.get(file.path)
            if parsed is None:
                parsed = ast.parse(source, filename=file.path)
                if sum(1 for _ in ast.walk(parsed)) > MAX_AST_NODES:
                    diagnostics.append(
                        Diagnostic("ast_node_limit", "AST exceeds node limit.", file.path)
                    )
                    unavailable.add(module)
                    skipped += 1
                    continue
                # Compiler scope validation only; no bytecode execution/imports/evaluation.
                symtable.symtable(source, file.path, "exec")
                syntax[file.path] = parsed
            modules[module].append(extract(file.path, module, parsed, source, namespace))
        except (SyntaxError, UnicodeError, LookupError, RecursionError, ValueError) as exc:
            diagnostics.append(
                Diagnostic(
                    "parse_error",
                    f"Cannot parse source: {type(exc).__name__}.",
                    file.path,
                    getattr(exc, "lineno", 0) or 0,
                )
            )
            unavailable.add(module)
            skipped += 1
    symbols, references, edges, resolved_diagnostics = resolve(modules, unavailable, namespace)
    diagnostics.extend(resolved_diagnostics)
    return Snapshot(
        tree,
        tuple(sorted(symbols, key=lambda s: (s.logical_id, s.id))),
        tuple(
            sorted(
                references,
                key=lambda r: (r.location.path, r.location.line, r.location.column, r.id),
            )
        ),
        tuple(sorted(edges, key=lambda e: (e.source_id, e.target_id, e.site.line, e.site.column))),
        tuple(sorted(diagnostics, key=lambda d: (d.path, d.line, d.code, d.message))),
        sum(len(group) for group in modules.values()),
        skipped,
        config.fingerprint,
        config.source_roots,
    )
