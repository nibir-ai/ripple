# Architecture

`cli.py` validates commands and delegates to application functions. `git_source.py`
discovers a repository and reads immutable tree/blob data under resource bounds.
`config.py` reads source-root data from immutable snapshot TOML or validated CLI flags.
`frontend.py` parses encoding-aware source and validates scope syntax with `symtable`.
`python_extract.py` inventories definition/import/binding scopes and unresolved syntax sites.
`python_resolve.py` maps lexical bindings and local imports into an evidence graph.
`models.py` holds frozen syntax, symbol, reference, edge and report records.
`matching.py` compares normalized definitions using explicit revision relations and grouped
relocation candidates. `git_source.py` also supplies bounded file diffs with blob provenance.
`analysis.py` applies those relations and performs iterative incoming-call
traversal. `output.py` renders that same report as sanitized human text; the report
serializer emits JSON. There is no UI-specific analyzer or persistence coupling.
`indexing.py` orchestrates exact graph reuse or changed-file syntax reuse with a full
semantic pass. `cache.py` owns SQLite migrations, publication and maintenance;
`cache_codec.py` validates typed snapshot/report JSON and bounded AST JSON.

```text
CLI → Git tree/blob source + source-root config → AST/scope facts → resolver → snapshot graph
                                                               ↓
before + after graphs + Git file diff → revision relations/changes → incoming CALLS → typed report
```

The graph algorithm consumes immutable snapshots without filesystem or Git side effects.
Compatible snapshots and syntax are stored outside source repositories in SQLite;
An interactive terminal interface is not yet implemented. Runtime dependencies
are the standard library (including SQLite) and the installed Git
executable. Development/build dependencies are isolated in virtual environments.

See `analyzer-semantics.md` for resolution and comparison rules.

The cache uses full repository re-resolution on graph misses. Persisted syntax is reusable
across trees; mutable lexical scopes and snapshot IDs are freshly extracted. Report
serialization excludes cache counters and timestamps, preserving byte-for-byte finding
parity across cached, incremental, forced and cache-disabled modes. See `cache.md`.
