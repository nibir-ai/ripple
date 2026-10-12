# Implemented semantics

Ripple parses Git blobs with host Python AST, validating scope declarations with `symtable`.
Modules derive from the longest matching snapshot source root, stripping `/__init__`.
See `configuration.md` for defaults/overrides and `python-support.md` for supported cases.
Definition spans include the decorator @ token, even for multiline decorators. Lines are
one-based; columns are zero-based UTF-8 byte offsets; end columns are exclusive.

Resolved rules:

- `python.same-module-function-v1`: unique, unconditional, undecorated module function;
  no conflicting module or local binding.
- `python.imported-function-call-v1`: absolute module from-import of such a function,
  including aliases, relative imports and module-qualified members, with unique mappings.
- `python.direct-from-import-v1`: import-only evidence; never traversed as a call.
- `python.lexical-function-v2`: unique nested/enclosing lexical definition.
- `python.lexical-method-v2`: unique lexical class member with no unsupported inheritance,
  custom class decorators, attribute hooks or conflicting bindings; dispatch caveat preserved.
- `python.class-constructor-v2`: class call only; does not manufacture constructor-method edges.
- Module/namespace imports and known builtin/stdlib external references use named v2 rules.

Resolution is deliberately flow-insensitive: any rebinding in scope blocks a claim.
This rejects some valid calls to avoid claiming imported/global targets for parameters,
local assignments, exception bindings and pattern bindings. Read-only global/nonlocal
declarations select the module/enclosing function; explicit writes invalidate their destination.
Class namespaces do not become method free-variable scopes. Missing names/re-exports,
lambda/comprehension calls, annotations/default evaluation and unknown object receivers
remain unresolved. Known stdlib/builtin references are external and never traversed.
Dynamic namespace access/wildcards block unique resolution; explicit known module/class/self
attribute writes invalidate affected members. Runtime mutation not established by supported
syntax remains a limitation. Method resolution carries explicit runtime-dispatch caveats.
Known decorators and class bases create DECORATES/INHERITS references with the definition
as source, never a CALLS edge. Resolved means static-policy evidence, not runtime certainty.

Bodies/signatures/decorators use position-free AST hashes. Whitespace and line shifts do
not change these hashes. Leading docstrings have their own non-propagating change facts.
Nested bodies are replaced with stubs only in their parent's hash; their headers remain.
Module/class facts are visible warnings, with bases/imports compared separately, without
caller-impact seeds. Unsupported definitions still carry syntax facts.

Unique path/qualname/kind identities match across revisions, followed by Git file rename
evidence for unique qualname/kind definitions. Copies never merge identities. Structural
function relocation candidates remain separate additions/removals. Changed duplicate
definitions stay unmatched with ambiguity diagnostics; equal ordered duplicate facts
suppress false changes without pairing identities. See `revision-comparison.md`.

Impact traverses incoming `CALLS` in both snapshots, preserving one shortest path per
origin/snapshot. A result may be `before_only`, `after_only` or `both`; old source locations
are never replaced by working-tree locations. Counts deduplicate only explicit match relations.
Moved callers retain old/new names and locations. Encountered alternative paths/cycles
set `additional_paths_possible`; this is not an exhaustive path count.
Paths list caller-to-callee steps toward the initiating change. Imports cannot seed propagation.
Cycles terminate; changed roots are not their own caller results. Reaching a limit marks
`truncated` with its exact reason. Partial coverage and no-result caveats remain visible.
Both snapshots contribute diagnostics/counts, even for identical trees.

No confidence percentages or test coverage claims are produced. Comparison relocation
candidates are separate from call-graph resolution and never create impact edges.
No target code runs and no analyzed files change.
