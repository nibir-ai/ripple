# Contributing

Install `.[dev]` in a virtual environment using the README commands. Run:

```console
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m build
```

Use the documented analyzer semantics and supported Python syntax as the behavior contract.
Every new resolution rule needs a positive and a counterexample fixture. Preserve
caller-to-callee direction, no target execution and revision-specific evidence.
Document semantic changes in user documentation and changelog. Do not regenerate golden reports
without reviewing the changed evidence. The internal Python API remains experimental.

Explorer interaction tests use Textual's headless pilot with real Git comparisons.
Check pane synchronization, focus, responsive widths and obsolete worker results when
changing the interface. Inspect a rendered wide and narrow terminal view for visual changes.
CI also runs explorer tests against the built wheel with its optional `tui` dependencies.
