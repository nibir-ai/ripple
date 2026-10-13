# CLI contract

`ripple --help`, `ripple --version`, `ripple index`, `ripple analyze` are implemented.
`ripple status`, `ripple doctor`, `ripple cache clear` and `ripple explain` are also implemented.
`--repo PATH`, `--format text|json`, `--strict` belong after the command.
Index accepts `--ref` (default HEAD). Analyze accepts `--base` (HEAD~1), `--head` (HEAD),
`--max-depth` (8), `--max-nodes` (5,000), both positive integers.
Analyze also accepts `--rename-threshold` (90), an integer from 50 through 100.
Traversal accepts `--max-edges` (100,000), `--max-paths-per-target` (16) and
`--query-timeout-ms` (10,000), all positive integers. The timeout maximum is 3,600,000 ms.
`--no-candidates` explicitly
selects the default resolved-only call policy. See `reports.md` for cap semantics.
Git shares the similarity setting for rename/copy detection; all copy relations remain
new identities. Detection is bounded by a 2,000-file exhaustive fallback limit,
20-second Git timeout and 8 MiB diff output cap. Rename evidence is heuristic.
Both commands accept repeated `--source-root PATH` overrides. Otherwise roots come from
the compared snapshot's `[tool.ripple].source_roots`, then automatic `src`/repository roots.
Root paths use `/`, are repository-relative and must contain supported source in the snapshot.
Each side uses its own immutable configuration. Invalid TOML/keys/roots exit 2.
Root commits require explicit refs; no merge-base or working-tree semantics are implicit.

Index persists snapshots and emits `ripple.index.v1` with syntax/graph facts. Analyze emits
`ripple.report.v1`, described by `src/ripple/schemas/report-v1.schema.json`.
One JSON document is written to stdout; fatal errors go to stderr without a partial document.
Use shell redirection to save a report. Paths are repository-relative; source snippets omitted.
Additive report fields include `file_changes`, `symbol_matches`, `match_candidates`,
old/new logical names and caller locations, and `additional_paths_possible`. Comparison
ambiguity diagnostics use `snapshot: comparison`. See `revision-comparison.md` for their semantics.

Index/analyze accept `--no-cache`, `--force` and `--cache-stats`. All commands accept
`--cache-dir BASE`, which always receives a workspace-specific namespace. Cache work
counters/warnings go to stderr; cached and full JSON findings remain identical. Status
reports committed HEAD staleness and saved analysis metadata. Doctor inspects Git/config
and cache integrity without creating/migrating caches, exiting 4 if unhealthy. Cache clear
deletes only active workspace rows and vacuums the owned database; it preserves source
files, other namespaces, owner markers and quarantines. See `cache.md` for exact behavior.

Exit codes: 0 success (impacts are information); 1 I/O failure; 2 invalid CLI usage;
3 Git/repository/ref/resource failure; 4 strict partial report; 130 interruption.
No color/interactive progress is emitted. Text escapes terminal controls.
`explore`, config and file-export commands are deferred.

Explain selects one impacted symbol by an exact name, `--symbol-id ID` or
`--path file.py:line`. It reads the latest completed workspace report by default;
`--analysis ID` selects a report ID from status. Stale cached reports require
`--allow-stale`. `--report report.json` reads portable report data without requiring
a repository or checking current HEAD. `--format json` emits a report projection
with one impact and its initiating changes, retaining original completeness and policy.
Invalid, absent or ambiguous selections exit 2 without partial stdout. See `reports.md`.
