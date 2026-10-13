# Terminal explorer

The interface takes visual cues from [Superfile](https://superfile.dev/): compact
bordered panels, a clear active pane and a muted midnight palette with lavender focus
accents. Plain text labels and standard box drawing keep it usable without icon fonts.

For the intended typography, install [JetBrains Mono](https://www.jetbrains.com/lp/mono/)
and select it in your terminal profile. In Windows Terminal, open Settings → your
PowerShell profile → Appearance → Font face and select **JetBrains Mono**; 12–14 pt
is a comfortable starting point. The equivalent profile JSON is:

```json
"font": { "face": "JetBrains Mono", "size": 13 }
```

See [Windows Terminal font settings](https://learn.microsoft.com/windows/terminal/customize-settings/profile-appearance#font-face)
for configuration details. Ripple uses the host terminal's font settings.

Install the optional interface with `pip install "ripple-impact[tui]"`, then run it
from an interactive terminal:

```console
ripple explore --repo /path/to/project
ripple explore --repo /path/to/project --base main --head HEAD
ripple explore --repo /path/to/project --analysis ANALYSIS_ID --allow-stale
ripple explore --report report.json --repo /path/to/project
ripple explore --repo /path/to/project --no-color
```

Without explicit revisions, the explorer opens the latest compatible saved report or
analyzes HEAD~1..HEAD. Obtain saved analysis IDs with `ripple status`. Exact saved
analyses reject changed HEAD trees, configuration or tool versions unless `--allow-stale`
is supplied. Allowed stale data carries a visible label; it retains its original trees.
Exported reports retain original provenance and can be opened outside a Git repository.
Source is unavailable if the attached repository lacks the recorded objects.

The three panes show changed symbols, potential effects and source/evidence. Selecting
a change updates its callers. Selecting a caller shows its call-site explanation and
immutable source context. Direct and transitive resolved calls, relocation candidates
and diagnostics have separate groups. Counts describe the full report; presentation
filters do not change analysis completeness. An empty caller group does not prove safety.

| Key | Action |
| --- | --- |
| Tab / Shift+Tab | Next / previous pane |
| 1 / 2 / 3 | Changes / potential effects / evidence |
| j / k or arrows | Move through rows, tree nodes or source text |
| Enter | Inspect selection; load the next result page |
| Space | Expand or collapse a tree group |
| / | Search symbols, packages and paths |
| Escape | Clear search or close a dialog |
| f | Filter changes by kind |
| p | Show full retained caller paths and provenance |
| s | Switch before/after evidence; retain the available side if only one exists |
| t | Related tests with call paths or weaker import-only evidence |
| d | Whole-comparison diagnostics and completeness reasons |
| c | Show source location as selectable text |
| r | Refresh the selected report source |
| ? | Keyboard reference |
| q | Quit; close a detail dialog when one is open |

Search accepts shortcut letters as ordinary text. Enter returns to the impact pane;
Escape clears search. Change filters and search combine. The change list shows up to
500 matching symbols; refine search to find others. Each impact group loads 200 rows
at a time. The diagnostics dialog shows its first 1,000 entries and explicitly labels
that cap; the tree supports searching and paging through the full report.

At 120 columns or wider all three panes are visible. From 80 through 119 columns,
changes and potential effects share the screen, with evidence on its own view.
Narrower terminals show one pane at a time. Selection survives resizing. Below
40 columns or 12 rows, a resize message replaces the panes. `NO_COLOR` or `--no-color`
disables color; labels also communicate evidence classes and staleness.

Report loading and immutable source reads run in background workers. Source previews
are debounced, capped at eight lines / 8 KiB and read from reported trees, including
before-only evidence. Dirty files never replace historical source. Obsolete results
cannot overwrite a new selection. Cancellation is cooperative: an already-running Git
command may finish within its 20-second timeout. Refresh reanalyzes the current refs;
for an exact saved analysis or exported file it rereads that same data.

No target code or tests run. Related tests describe static associations, not sufficient
coverage. Partial/truncated reports keep their explicit reasons visible. Load errors
leave the interface quittable and return their error code. The terminal is restored on
quit or failure. For pipes and automation, use `analyze --format json` or `explain`.
