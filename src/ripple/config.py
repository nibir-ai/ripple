"""Snapshot source mapping; configuration is data from each immutable snapshot."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from ripple.errors import RippleError
from ripple.git_source import GitSource, SourceFile
from ripple.models import Diagnostic, stable_id


@dataclass(frozen=True)
class SourceConfig:
    source_roots: tuple[str, ...]
    provenance: str = "automatic"

    @property
    def fingerprint(self) -> str:
        return stable_id("source-roots-v1", *sorted(self.source_roots))

    def module_for(self, path: str) -> str | None:
        matching = [
            root for root in self.source_roots if root == "." or path.startswith(root + "/")
        ]
        if not matching:
            return None
        root = max(matching, key=lambda r: len(r) if r != "." else 0)
        relative = path if root == "." else path[len(root) + 1 :]
        parts = relative[:-3].split("/")
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) or "__root__"


def validate_roots(value: object, paths: list[str]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise RippleError("RPL-CONFIG-001", "source_roots must be a nonempty list of paths.", 2)
    roots = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or "\\" in item
            or ":" in item
            or item.startswith("/")
            or any(ord(c) < 32 for c in item)
            or ".." in item.split("/")
        ):
            raise RippleError(
                "RPL-CONFIG-001", "Source roots must be safe relative POSIX paths.", 2
            )
        root = str(PurePosixPath(item))
        if root != "." and not any(path.startswith(root + "/") for path in paths):
            raise RippleError(
                "RPL-CONFIG-002", f"Source root {root!r} has no source in snapshot.", 2
            )
        if root not in roots:
            roots.append(root)
    return tuple(sorted(roots))


def load_config(
    source: GitSource,
    tree: str,
    files: list[SourceFile],
    overrides: tuple[str, ...] | None = None,
    excluded: list[Diagnostic] | None = None,
) -> SourceConfig:
    paths = [f.path for f in files] + [d.path for d in excluded or [] if d.path.endswith(".py")]
    data = source.read_optional(tree, "pyproject.toml", 65536)
    settings: dict[str, object] = {}
    if data is not None:
        try:
            parsed = tomllib.loads(data.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise RippleError("RPL-CONFIG-001", "Invalid snapshot pyproject.toml.", 2) from exc
        tool = parsed.get("tool", {})
        if not isinstance(tool, dict) or not isinstance(tool.get("ripple", {}), dict):
            raise RippleError("RPL-CONFIG-001", "[tool.ripple] must be a TOML table.", 2)
        settings = tool.get("ripple", {})
        if set(settings) - {"source_roots"}:
            raise RippleError(
                "RPL-CONFIG-001",
                "Unsupported [tool.ripple] key; only source_roots is supported.",
                2,
            )
    if overrides is not None:
        return SourceConfig(validate_roots(overrides, paths), "cli")
    if "source_roots" in settings:
        return SourceConfig(validate_roots(settings["source_roots"], paths), "snapshot_pyproject")
    return SourceConfig(("src", ".") if any(p.startswith("src/") for p in paths) else (".",))
