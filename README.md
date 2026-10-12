# Ripple

See potential ripple effects of Python code changes, with source evidence, from your terminal.
Ripple compares immutable Git trees, finds changed functions and follows statically
supported caller relationships. Analysis runs locally without executing target code.

**Status: Development preview (`0.1.0.dev4`).** Revision matching, SQLite
snapshots and content-addressed syntax reuse are implemented. The TUI and full v0.1
release are not yet available.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\ripple --help
.venv\Scripts\python examples/demo.py --destination .demo
.venv\Scripts\ripple analyze --repo .demo
.venv\Scripts\ripple analyze --repo .demo --format json
.venv\Scripts\python examples/demo.py --destination .demo-package --package
.venv\Scripts\ripple analyze --repo .demo-package --strict
.venv\Scripts\python examples/demo.py --destination .demo-rename --rename
.venv\Scripts\ripple analyze --repo .demo-rename
.venv\Scripts\ripple status --repo .demo-package
.venv\Scripts\ripple doctor --repo .demo-package
.venv\Scripts\ripple analyze --repo .demo-package --cache-stats
```

On Linux/macOS use `.venv/bin/python` and `.venv/bin/ripple`.
For your own Python Git repository:

```console
ripple index --repo /path/to/project --ref HEAD
ripple analyze --repo /path/to/project --base main --head HEAD
```

The generated demo changes `service.calculate_total` and reports `api.checkout` as
a direct potential caller and `api.endpoint` as a transitive potential caller.
Every path records `caller --CALLS--> callee`, exact call-site location, resolution
rule and before/after tree provenance. This is a supported static relationship,
not a prediction that behavior will break.

Uncommitted changes are excluded. Compatible snapshots are cached outside the repository.
Changed trees reuse unchanged syntax and rebuild all resolution facts for correctness.
Use `--no-cache` for a clean full analysis and `--force` for an atomic full cache rebuild.
Find cache location/staleness with `status`; clear only workspace facts with `cache clear`.
Lexical/nested functions, direct and relative imports, module aliases and narrow lexical
self/cls/class-name method cases resolve. Source roots recognize `src/` automatically or
come from snapshot `[tool.ripple].source_roots` and repeated CLI `--source-root` flags.
Unknown receivers, shadowed bindings and unsupported dispatch remain visible diagnostics.
Git file renames preserve unique definitions and both source locations. Copies create
new symbols. Structural function relocation candidates keep separate add/remove facts.
Docstring-only changes are visible without caller propagation; nested body changes belong
to their own definition. Module/class changes are visible warnings. See the rename demo
for before-only callers and unresolved head imports; `--strict` deliberately exits 4 there.
Method paths explicitly qualify runtime dispatch. No callers found does not prove safety.
`--strict` exits 4 when analysis is partial. Configuration is never read from dirty source files.

- [CLI contracts](docs/cli.md)
- [Analyzer semantics and limitations](docs/analyzer-semantics.md)
- [Architecture](docs/architecture.md)
- [Revision comparison](docs/revision-comparison.md)
- [Cache behavior](docs/cache.md)
- [Supported Python syntax](docs/python-support.md)
- [Source-root configuration](docs/configuration.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [License](LICENSE)
