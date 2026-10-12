# Snapshot cache

Ripple caches snapshots by default. On Windows the default base is `%LOCALAPPDATA%/Ripple`;
on Linux/macOS it is `$XDG_CACHE_HOME/ripple`, falling back to `~/.cache/ripple`.
A hash of the normalized repository root names the workspace subdirectory.
`ripple status --repo PATH` shows the exact database path, size, snapshot/syntax counts,
roots, parser/analyzer versions, last index times and saved analysis metadata.

```powershell
ripple index --repo PATH
ripple analyze --repo PATH --cache-stats
ripple index --repo PATH --force
ripple analyze --repo PATH --no-cache
ripple status --repo PATH --format json
ripple doctor --repo PATH --format json
ripple cache clear --repo PATH
```

All commands accept `--cache-dir BASE`; a workspace namespace is always appended.
`--force` bypasses both graph and syntax reuse, publishing the replacement only after
success. `--no-cache` bypasses reads/writes and creates no cache. Optional work counters
and cache warnings go to stderr, leaving JSON findings unchanged between modes.
The `parsed_files` work counter counts parse attempts; snapshot coverage counts retain
their existing meaning. `resolution_passes` counts full repository semantic passes.

An exact key hit reuses the graph. A changed tree reuses content-addressed AST syntax
and source bytes, parses changed/new blobs, then re-extracts scopes and resolves all files.
This prevents stale edges after an export, import, class binding or root mapping changes.
No persistent working-tree overlay or fine-grained resolution closure is implemented.

`status` compares HEAD with the current snapshot/config/rule/runtime/limit key. Saved
analysis staleness compares its head snapshot key with that key. These checks concern
immutable committed HEAD, so dirty source files do not make a committed analysis stale.
Status/doctor inspection does not create or migrate caches. Doctor checks Git/config,
SQLite integrity/foreign keys, compatible snapshot checksums/DTOs/inventory, syntax
checksums and saved reports. Unhealthy doctor results exit 4. Missing cache alone is
healthy and suggests indexing. Normal analysis remains usable if caching fails.

Owned schema mismatches and unreadable databases are preserved as
`index.sqlite3.quarantine.*` before automatic index-time rebuilding. Invalid individual
records fall back to reparse/rebuild and are replaced after successful publication.
Use `index --force` for current-tree repair or scoped `cache clear` for obsolete records.
The recovery directory lock prevents simultaneous quarantine operations; a leftover
recovery lock after an interrupted repair may require inspection and manual removal.
Unowned or symlink/reparse caches are refused instead of repaired.

Clearing removes only this workspace's active cached rows and vacuums its database.
Source repositories, other workspace namespaces and unrelated files remain untouched.
Owner markers, the empty database and quarantined backups remain. Cache data contains
source bytes, ASTs, repository structure and reports; keep custom bases private. There is
no automatic expiration, eviction, backup removal, network cache or encryption in this preview.

The schema is currently v3. Migrations from v1/v2 add tables transactionally and preserve
complete snapshots. Source-input, parser and payload limits still apply; the cache JSON
payload limit is 64 MiB and SQLite operations have a one-second busy timeout. Parsing is
not process-isolated. SQLite crash safety also relies on the filesystem's locking and flush
behavior; the tests establish process-interruption behavior on the local Windows environment.

See `architecture.md` for the indexing pipeline and `tools/cache_benchmark.py` for a reproducible cache benchmark.
