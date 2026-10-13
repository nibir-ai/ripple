# Changelog

## 0.1.0.dev6 — 2026-10-13

Added an optional Textual terminal explorer with synchronized changes, caller groups
and immutable source evidence. Keyboard navigation, search, change filters, retained
paths, related tests, diagnostics and location dialogs share the validated report model.
Layouts adapt to wide, medium and narrow terminals. Saved analyses reject stale data
unless explicitly allowed; exported reports remain portable without source access.
Background workers keep navigation responsive and discard obsolete reads. Source excerpts
and blob retention are bounded. The standard-library headless runtime is unchanged.

## 0.1.0.dev5 — 2026-10-13

Added saved-report evidence inspection by symbol name, ID or source location, with
explicit ambiguity errors and stale-cache checks. Impact traversal now limits edge
work, query time and retained paths per target and reports every reached cap.
Broad caller lists have compact text groups while retaining all findings in JSON.
Related tests distinguish direct/transitive call paths from weaker test-module imports.
Reports validate evidence origins, direction, provenance and summary counts before
publication or inspection. Added a related-test demonstration and additive schema fields.

## 0.1.0.dev4 — 2026-10-12

Versioned SQLite snapshots and analysis metadata, additive migrations,
atomic staging/publication, exact compatibility keys and content-addressed AST/source
reuse. Changed snapshots re-resolve all repository facts; cached/full findings retain
parity. Added status, doctor, scoped cache clear, no-cache/force controls and work counters.
Owned corrupt/incompatible databases are quarantined; cache failures preserve valid full
results. Added interruption, corruption, migration and parity fixtures plus a reproducible
64-file cache benchmark. The TUI, fine-grained resolution invalidation and full release
hardening remain deferred.

## 0.1.0.dev3 — 2026-10-12

Bounded raw Git file diffs with rename/copy similarity and blob provenance;
explicit revision match relations and grouped structural candidates; ambiguity diagnostics;
docstring and module/class change facts; nested body normalization; matched caller
deduplication with both names/locations and additional-path/cycle notices. Added a real
rename/obsolete-import demo and additive report-v1 fields. Structural candidates never
merge identities, copies remain additions, and persistence/TUI remain deferred.

## 0.1.0.dev2 — 2026-10-12

Explicit lexical scopes, nested functions, read-only global/nonlocal lookup,
binding invalidation, absolute/relative/module imports and aliases, automatic src layouts,
snapshot TOML and CLI source roots, conservative self/cls/class-name methods, known external
references, decorator/base evidence and exact decorator spans. Call paths carry resolution
limitations; snapshots/report comparisons record source scope and configuration fingerprints.
Added scope/import/method/configuration regression suites and a real package demo.
Simple re-exports, inheritance dispatch, instance type inference remain unsupported.

## 0.1.0.dev1 — 2026-10-12

Initial development preview: installable CLI; immutable Git input; Python AST definitions,
imports and call sites; conservative direct-call resolution; changed-function comparison;
reverse caller evidence; deterministic JSON/text reports; executable demo and regression tests.
Persistent caching, advanced matching, test suggestions and TUI remain unimplemented.
