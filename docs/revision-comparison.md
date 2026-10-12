# Revision comparison rules

| Case | Implemented behavior |
|---|---|
| Same tree | No changes or file diff; both sides still contribute coverage counts |
| Whitespace/comments/line shifts | Position-free AST equality; no runtime body change |
| Leading docstring | `docstring_modified`, visible without caller propagation |
| Nested definition body | Changes its own hash; enclosing body retains only nested binding/header facts |
| Body/signature/decorator | Independent change facts; callable changes seed both relevant graphs |
| Class bases | `bases_modified` warning; no speculative downstream method dispatch |
| Module imports/body, class body | Visible facts; no blanket import or class propagation |
| Unique path/qualname/kind | Explicit cross-revision match relation |
| Git file rename | Match same unique qualname/kind under Git similarity evidence; `symbol_relocated` |
| Copied file | New definitions; never merge with original identities |
| Same-file function rename | Structural relocation candidate when full published structure agrees; add/remove facts retained |
| Cross-file symbol move | Candidate if structure/parent agrees; no automatic graph merge |
| Several identical candidates | Grouped ambiguity diagnostic; no arbitrary target selection |
| Duplicate definitions | Changed duplicates unmatched; equal ordered facts suppress false changes without pairing identities |
| Parse/exclusion failure | Do not invent additions/removals for unavailable paths or their Git rename partners |
| Deleted/obsolete reference | Before graph retains caller evidence; unsupported head reference remains a diagnostic |
| Moved caller | Explicit matches deduplicate counts while retaining both names and locations |
| Diamond/cycle | Deterministic shortest path per origin/revision; additional-path/cycle notice when encountered |

The `file_changes` array stores all Git tree file statuses and full old/new blob IDs,
including non-Python files. Python symbol facts remain limited to indexed source.
Rename/copy similarity uses the same threshold because Git shares that setting.
Git relations are heuristics. They do not prove the developer's intent or runtime identity.

`symbol_matches` contains definite relations under the published static policy.
`match_candidates` contains resemblance evidence and grouped names/IDs, never resolved
call edges. `candidate_references` and `candidate_impact_count` still concern graph
resolution and remain zero; `relocation_candidate_count` counts comparison candidate groups.

Caller propagation covers functions and methods. Class/module additions, deletions,
relocations and modifications are visible warnings; constructor-call downstream effects,
inheritance effects, call-shape compatibility checks, and comprehensive reference churn
remain deferred. Docstring changes can matter for reflection or tooling despite having
no caller-impact seeds. The documented Python resolution limitations still apply.

There is no merge-base inference, working-tree overlay or TUI. The
persistent cache is described in `cache.md`.
Before/after are the exact user-selected Git trees. Source-root configuration is read
independently from each snapshot; changed module mappings retain same-path identities
and expose the old/new logical names.
