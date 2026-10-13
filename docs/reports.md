# Evidence reports and related tests

Ripple reports potential effects supported by resolved static call relationships.
`python-callers-v1` follows incoming CALLS edges independently in the before and
after trees. Depth 1 is a direct caller; larger depths are transitive callers.
One deterministic shortest path is retained per initiating change and revision,
subject to the path cap. Counts deduplicate explicit revision matches. Relocation
candidates remain separate comparison evidence and never create call impacts;
unknown and external references never inflate resolved counts. Candidate call
traversal is not implemented. `--no-candidates` records the existing default choice.

## Limits and presentation

Default bounds are depth 8, 5,000 distinct impacted identities, 100,000 edge-work
operations, 16 stored evidence paths per target and 10,000 milliseconds of caller-query
time, configurable up to 3,600,000 milliseconds. Edge work counts both adjacency
construction and traversed edges across revisions
and initiating changes, so it is not a count of unique edges. The monotonic deadline
covers caller traversal, including adjacency preparation; it does not time parsing,
revision comparison, test association, serialization or the entire CLI process.
Git and source parsing have their separate documented limits.

Reached caps appear in `completeness.reasons` as `max_depth`, `max_nodes`, `max_edges`,
`max_paths_per_target` or `query_timeout`. The report becomes partial and truncated;
`--strict` exits 4. Stored paths and origin IDs describe retained evidence only. Other
paths or origins may exist when capped. Query-time truncation can vary with machine
load; completed uncapped findings retain deterministic ordering and cache parity.

Text output shows at most 20 callers per category when the category exceeds that
size. `impact_groups` records the total and displayed counts. This presentation limit
does not truncate analysis: JSON retains every collected impact, and `explain` can
inspect each one. Cycles terminate; alternative paths are flagged when encountered,
without claiming an exhaustive path inventory.

## Inspecting evidence

```console
ripple analyze --repo /path/to/project
ripple explain pkg.api.endpoint --repo /path/to/project
ripple explain --symbol-id SYMBOL_ID --repo /path/to/project --format json
ripple explain --path pkg/api.py:42 --repo /path/to/project
ripple explain pkg.api.endpoint --report report.json
```

Exact names include old and new names for relocated callers. Ambiguous names or
locations list the matching symbol IDs and exit 2. Saved-cache inspection refuses
reports whose head tree, effective default source-root configuration or tool version
differs from the current workspace. Use `--allow-stale` to inspect original evidence
explicitly; source paths and tree IDs remain those of the saved revisions. Reports
produced with CLI source-root overrides may require that historical-inspection flag.
Dirty source files do not change an immutable HEAD report.

`status --format json` exposes the last `analysis_id`; `--analysis ID` selects it
exactly. Exported `--report` input is a deliberate historical data selection and does
not compare against the local workspace. Input is bounded to 64 MiB, checked for
duplicate fields, typed records and connected call evidence. It is never executed.
This verifies internal consistency, not the authenticity of externally supplied data.

## Related tests

Ripple recognizes top-level `test_*` functions and `Test*` class `test_*` methods
in `tests/` paths, `test_*.py` files or `*_test.py` files. Async definitions follow the
same conventions. Nested test-like names and ordinary production functions with
test-like names are not selected. Supported test calls reuse the same impact paths.

- `direct_static_call`: a recognized test has a resolved call to changed code.
- `transitive_static_call`: a recognized test reaches changed code through a call path.
- `import_reference_only`: a test module imports a changed definition. This weaker
  association identifies the module, not an individual test. Import evidence retains
  its revision, tree, source position, named resolution rule and initiating changes.

Test associations are ordered by evidence class, then name and provenance; no numeric
risk or coverage score is inferred. `summary.related_test_count` counts recognized
tests with call paths, already included in caller counts. The separate
`related_test_import_module_count` counts modules with weaker import associations.
Import evidence is deduplicated only under explicit revision matches.

Tests are never imported, collected or run. Fixture injection, dynamic registration,
parameterization, inherited test dispatch, aggregate coverage, mere filename similarity
and changed tests without a supported downstream path do not establish a test association.
Suggestions are related tests, not a sufficient test suite or proof of runtime coverage.

The bundled `ripple.report.v1` JSON schema defines additive test, grouping and limit
fields. Runtime validation also checks connected paths, initiating changes, revision
trees, resolved categories and counts. User-facing text and JSON share the same report.
