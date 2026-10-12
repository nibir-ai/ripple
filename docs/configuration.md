# Snapshot source-root configuration

Ripple supports one repository configuration file: the selected Git tree's `pyproject.toml`.
Only `source_roots` is accepted in `[tool.ripple]`; unsupported keys produce a clear error.
Other project/build metadata is parsed as TOML data and never executed.

```toml
[tool.ripple]
source_roots = ["src", "tests"]
```

Precedence: repeated command `--source-root` flags, snapshot TOML, automatic defaults.
Defaults are `src` plus `.` when snapshot Python sources exist under `src/`, otherwise `.`.
The longest matching root maps a file once, so `src/pkg/m.py` maps to `pkg.m`, not
`src.pkg.m`. `__init__.py` maps to its package; namespace packages need no initializer.

```console
ripple index --repo /path/to/repo --source-root packages/api/src --source-root tests
ripple analyze --repo /path/to/repo --base main --head HEAD --source-root src --source-root .
```

Explicit roots restrict scope: Python files outside them are diagnosed as exclusions.
Roots use POSIX `/`, are relative to the Git root, and must contain supported source in
each selected snapshot, including source skipped by input limits. Absolute, drive-qualified,
parent-traversing, empty, control-character and backslash paths are rejected. Duplicate
roots normalize and sort. Module collisions across roots remain unresolved; root ordering
does not silently choose a winner. Direct package re-exports remain unsupported.

Configuration must be a regular Git blob of at most 64 KiB; symlink config is rejected.
Malformed TOML and unknown Ripple keys fail with `RPL-CONFIG-001`; absent root source uses
`RPL-CONFIG-002`. Invalid config exits 2 and writes no partial JSON to stdout.
Dirty working-tree configuration is ignored. Base and head each use their own configuration,
unless CLI roots override both. Fingerprints and effective roots appear in JSON metadata.
No user config, environment `sys.path`, imported setup code or installed target packages are used.
