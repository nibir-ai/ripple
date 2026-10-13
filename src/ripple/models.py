"""Immutable syntax, graph and report records. Columns are zero-based UTF-8 bytes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

State = Literal["resolved", "candidate", "unresolved", "external", "excluded"]
Side = Literal["before", "after"]


def stable_id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Location:
    path: str
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class Symbol:
    id: str
    logical_id: str
    display_name: str
    module: str
    qualname: str
    kind: str
    location: Location
    body_hash: str
    signature_hash: str
    decorators_hash: str
    supported: bool
    docstring_hash: str = ""


@dataclass(frozen=True)
class Reference:
    id: str
    source_id: str
    syntax_kind: str
    expression: str
    location: Location
    resolution_state: State
    resolver_rule: str
    explanation: str
    target_id: str | None = None


@dataclass(frozen=True)
class Edge:
    source_id: str
    target_id: str
    edge_type: str
    site: Location
    resolver_rule: str
    explanation: str = ""


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str
    line: int = 0


@dataclass(frozen=True)
class Snapshot:
    tree: str
    symbols: tuple[Symbol, ...]
    references: tuple[Reference, ...]
    edges: tuple[Edge, ...]
    diagnostics: tuple[Diagnostic, ...]
    parsed_files: int
    skipped_files: int
    config_fingerprint: str = ""
    source_roots: tuple[str, ...] = (".",)


@dataclass(frozen=True)
class Change:
    change_id: str
    kind: str
    display_name: str
    before_symbol_id: str | None
    after_symbol_id: str | None
    before: Location | None
    after: Location | None
    match_rule: str
    before_display_name: str | None = None
    after_display_name: str | None = None


@dataclass(frozen=True)
class FileChange:
    status: str
    before_path: str | None
    after_path: str | None
    before_blob: str | None
    after_blob: str | None
    similarity: int | None = None


@dataclass(frozen=True)
class SymbolMatch:
    before_symbol_id: str
    after_symbol_id: str
    match_rule: str


@dataclass(frozen=True)
class MatchCandidate:
    before_symbol_ids: tuple[str, ...]
    after_symbol_ids: tuple[str, ...]
    before_names: tuple[str, ...]
    after_names: tuple[str, ...]
    match_rule: str
    explanation: str


@dataclass(frozen=True)
class Step:
    edge_type: str
    caller: str
    callee: str
    site: Location
    resolver_rule: str
    explanation: str = ""


@dataclass(frozen=True)
class EvidencePath:
    origin_change_id: str
    snapshot: Side
    tree: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Impact:
    symbol_id: str
    display_name: str
    category: str
    depth: int
    provenance: str
    resolution_state: str
    origin_change_ids: tuple[str, ...]
    evidence_paths: tuple[EvidencePath, ...]
    before: Location | None = None
    after: Location | None = None
    before_display_name: str | None = None
    after_display_name: str | None = None
    additional_paths_possible: bool = False


@dataclass(frozen=True)
class Policy:
    id: str = "python-callers-v1"
    max_depth: int = 8
    max_nodes: int = 5000
    include_candidates: bool = False
    max_edges: int = 100000
    max_paths_per_target: int = 16
    query_timeout_ms: int = 10000

    def __post_init__(self) -> None:
        if self.id != "python-callers-v1" or self.include_candidates:
            raise ValueError("Unsupported impact policy or candidate traversal")
        if any(
            type(n) is not int or n < 1
            for n in (
                self.max_depth,
                self.max_nodes,
                self.max_edges,
                self.max_paths_per_target,
                self.query_timeout_ms,
            )
        ):
            raise ValueError("Policy limits must be positive integers")
        if self.query_timeout_ms > 3600000:
            raise ValueError("Query timeout must not exceed 3600000 ms")


@dataclass(frozen=True)
class Completeness:
    status: str
    parsed_files: int
    skipped_files: int
    unresolved_references: int
    candidate_references: int
    truncated: bool
    reasons: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Comparison:
    base_ref: str
    base_tree: str
    head_ref: str
    head_tree: str
    base_source_roots: tuple[str, ...] = (".",)
    head_source_roots: tuple[str, ...] = (".",)


@dataclass(frozen=True)
class TestImport:
    snapshot: Side
    tree: str
    site: Location
    resolver_rule: str
    origin_change_ids: tuple[str, ...]


@dataclass(frozen=True)
class RelatedTest:
    symbol_id: str
    display_name: str
    evidence_class: str
    provenance: str
    origin_change_ids: tuple[str, ...]
    evidence_paths: tuple[EvidencePath, ...]
    import_evidence: tuple[TestImport, ...] = ()
    before: Location | None = None
    after: Location | None = None


@dataclass(frozen=True)
class ImpactGroup:
    category: str
    count: int
    displayed_count: int


@dataclass(frozen=True)
class Report:
    schema: str
    tool: dict[str, str]
    repository: dict[str, str]
    comparison: Comparison
    policy: Policy
    summary: dict[str, int]
    changes: tuple[Change, ...]
    impacts: tuple[Impact, ...]
    diagnostics: tuple[dict[str, str | int], ...]
    completeness: Completeness
    file_changes: tuple[FileChange, ...] = ()
    symbol_matches: tuple[SymbolMatch, ...] = ()
    match_candidates: tuple[MatchCandidate, ...] = ()
    related_tests: tuple[RelatedTest, ...] = ()
    impact_groups: tuple[ImpactGroup, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, sort_keys=True, indent=2) + "\n"
