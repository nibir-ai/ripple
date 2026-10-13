# Security and privacy

Ripple treats source files, filenames and Git refs as data. Analysis never imports
target Python code, invokes its tests, evaluates decorators or runs project build hooks.
Git receives argument arrays with timeout and bounded pipe readers; repository selection
uses `--repo`/cwd rather than inherited GIT_DIR or GIT_WORK_TREE. No checkout or writes
to analyzed repositories occur. Symlink blobs and submodules are excluded and diagnosed.

Ripple reads root configuration from immutable Git blobs with a 64 KiB guard, rejects
symlink config and unsafe source-root paths, and never imports target packages to resolve
imports. `symtable` validates lexical syntax without running bytecode. AST and standard-library
resolution still do not execute target code.

Input defaults: 2 MiB/file, 32 MiB/snapshot, 2,000 Python entries, 100,000 AST nodes/file,
8 MiB Git tree output, 64 KiB Git stderr, 20 seconds per Git process. Impact defaults:
depth 8 and 5,000 unique results, configurable via CLI. Parsing is not process-isolated;
this preview is not a hardened service for arbitrary hostile repositories. Caller queries
have edge-work, path and monotonic time budgets. These are cooperative limits, not
process isolation or a deadline for the entire CLI operation.

Raw tree diffs disable external diff and text converters. Full blob IDs and
NUL-separated paths are parsed as data. Rename/copy detection has a 2,000-file
exhaustive fallback limit and the same bounded output and process timeout. Structural
relocation candidates are grouped by hashes; no all-pairs fuzzy search or target code
execution is used. Copies and uncertain candidates never merge graph identities.

Terminal controls and bidirectional format controls are escaped in text. JSON uses
standard serialization. Semantic reports use portable paths and omit source excerpts
and absolute home paths; status explicitly shows the local cache path. Reports can still
reveal repository structure. The CLI has no report-file
writer, telemetry, networking, editor integration, shell hooks or background process.

Install/build tooling executes this trusted project's packaging, independently of the
analysis trust boundary. Dependency and publishing checks remain release gates.
No reporting endpoint is configured yet; do not disclose sensitive source in public issues.

Ripple stores source bytes, AST JSON, graphs and reports in an owned per-workspace user
cache outside source repositories. Checksummed typed JSON replaces executable object
serialization. Cache reads never use pickle/eval or execute target code. Source bytes
are verified against Git blob identity; incompatible keys cannot be reused. SQLite
publication is transactional with foreign keys and bounded payloads. Cache warnings and
optional work counters go to stderr, outside the semantic report.

Symlink/reparse cache paths and unowned databases are refused. New POSIX workspace
directories and database/marker files use private modes; Windows inherits user cache
access controls. Keep custom cache bases private. Read-only inspection does not create
or migrate databases. Owned corrupt/incompatible databases are quarantined rather than
overwritten. Scoped cache clear deletes active workspace rows only; it never recursively
deletes directories, source files or other workspace caches. Quarantines retain source
data until manually removed. There is no automatic eviction, encryption or network cache.

The optional explorer reads source only from immutable report tree IDs through the same
bounded Git interface. It never opens dirty source files or runs an editor, clipboard
command or target tests. Source context contains at most eight lines and 8 KiB of text;
long lines are clipped around the selected byte column. A session retains at most eight
source blobs (16 MiB). Text is rendered literally after terminal-control sanitization.
Canceled workers discard obsolete results; a running Git command may finish within its
existing 20-second process timeout. Textual restores the terminal on quit or failure.
