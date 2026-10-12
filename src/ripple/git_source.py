"""Read Git trees and blobs; never check out or execute analyzed sources."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ripple.errors import RippleError
from ripple.models import Diagnostic, FileChange

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 2000
MAX_TREE_BYTES = 8 * 1024 * 1024
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".ripple",
}


def run_git(cwd: Path, args: list[str], limit: int = MAX_TREE_BYTES) -> bytes:
    # Disable replace objects and inherited repository overrides: cwd selects the repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
    )
    try:
        process = subprocess.Popen(
            ["git", "--no-pager", *args],
            cwd=cwd,
            env=env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise RippleError(
            "RPL-GIT-001", "Git unavailable or repository path inaccessible."
        ) from exc
    output: list[bytes] = []
    errors: list[bytes] = []
    overflow = threading.Event()

    def consume(stream: BinaryIO, chunks: list[bytes], cap: int) -> None:
        total = 0
        while True:
            data = stream.read(65536)
            if not data:
                break
            total += len(data)
            if total > cap:
                overflow.set()
                process.kill()
                break
            chunks.append(data)

    readers = [
        threading.Thread(target=consume, args=(process.stdout, output, limit), daemon=True),
        threading.Thread(target=consume, args=(process.stderr, errors, 65536), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise RippleError("RPL-GIT-003", "Git timed out after 20 seconds.") from exc
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
    if overflow.is_set():
        raise RippleError("RPL-GIT-004", "Git output exceeded the resource limit.")
    if process.returncode:
        raise RippleError(
            "RPL-GIT-002", "Git operation failed; check repository and revision refs."
        )
    return b"".join(output)


@dataclass(frozen=True)
class SourceFile:
    path: str
    contents: bytes


@dataclass(frozen=True)
class SourceEntry:
    path: str
    blob: str
    size: int


class GitSource:
    def __init__(self, directory: Path):
        try:
            root = run_git(directory, ["rev-parse", "--show-toplevel"])
        except RippleError as exc:
            raise RippleError("RPL-GIT-001", "Not inside an accessible Git working tree.") from exc
        self.root = Path(os.fsdecode(root).rstrip("\r\n"))

    def resolve(self, ref: str) -> str:
        if not ref or ref.startswith("-") or any(ord(c) < 32 for c in ref):
            raise RippleError("RPL-GIT-002", "Invalid revision ref; use a commit or tree ref.")
        tree = run_git(self.root, ["rev-parse", "--verify", "--end-of-options", ref + "^{tree}"])
        oid = tree.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise RippleError("RPL-GIT-002", "Revision did not resolve to a Git tree.")
        return oid

    def inventory(self, tree: str) -> tuple[list[SourceEntry], list[Diagnostic]]:
        entries = run_git(self.root, ["ls-tree", "-r", "-l", "-z", "--full-tree", tree])
        files: list[SourceEntry] = []
        diagnostics: list[Diagnostic] = []
        total = 0
        count = 0
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, blob, size = metadata.split()
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if kind == b"commit":
                diagnostics.append(Diagnostic("submodule_excluded", "Submodule not indexed.", path))
                continue
            if not path.endswith(".py"):
                continue
            count += 1
            reason = ""
            if mode == b"120000":
                reason = "symlink_excluded"
            elif any(part in EXCLUDED_PARTS for part in path.split("/")):
                reason = "default_exclusion"
            elif count > MAX_FILES:
                reason = "file_count_limit"
            elif int(size) > MAX_FILE_BYTES:
                reason = "file_size_limit"
            elif total + int(size) > MAX_TOTAL_BYTES:
                reason = "aggregate_size_limit"
            if reason:
                diagnostics.append(Diagnostic(reason, "Source excluded by input policy.", path))
                continue
            total += int(size)
            files.append(SourceEntry(path, blob.decode("ascii"), int(size)))
        return files, diagnostics

    def read_entry(self, entry: SourceEntry) -> SourceFile:
        return SourceFile(
            entry.path, run_git(self.root, ["cat-file", "blob", entry.blob], MAX_FILE_BYTES)
        )

    def files(self, tree: str) -> tuple[list[SourceFile], list[Diagnostic]]:
        entries, diagnostics = self.inventory(tree)
        return [self.read_entry(entry) for entry in entries], diagnostics

    def diff(self, before: str, after: str, threshold: int = 90) -> tuple[FileChange, ...]:
        """Bounded, NUL-delimited raw tree diff; no external diff or text converters."""
        if not 50 <= threshold <= 100:
            raise RippleError("RPL-CONFIG-001", "Rename threshold must be 50 through 100.", 2)
        data = run_git(
            self.root,
            [
                "-c",
                "diff.renameLimit=2000",
                "-c",
                "diff.algorithm=myers",
                "diff-tree",
                "--no-commit-id",
                "-r",
                "--raw",
                "-z",
                "--no-abbrev",
                "--no-ext-diff",
                "--no-textconv",
                f"--find-renames={threshold}%",
                f"--find-copies={threshold}%",
                "--find-copies-harder",
                "-l2000",
                before,
                after,
                "--",
            ],
        )
        fields = iter(data.split(b"\0"))
        result = []
        for header in fields:
            if not header:
                continue
            try:
                old_mode, new_mode, old_blob, new_blob, raw_status = header.split()
                if not old_mode.startswith(b":"):
                    raise ValueError("invalid raw header")
                status = raw_status[:1].decode("ascii")
                first = next(fields).decode("utf-8", "surrogateescape")
                second = (
                    next(fields).decode("utf-8", "surrogateescape")
                    if status in {"R", "C"}
                    else first
                )
                result.append(
                    FileChange(
                        {
                            "A": "added",
                            "D": "deleted",
                            "M": "modified",
                            "R": "renamed",
                            "C": "copied",
                            "T": "type_changed",
                        }.get(status, "unknown"),
                        None if status == "A" else first,
                        None if status == "D" else second,
                        None if set(old_blob) == {48} else old_blob.decode("ascii"),
                        None if set(new_blob) == {48} else new_blob.decode("ascii"),
                        int(raw_status[1:]) if status in {"R", "C"} else None,
                    )
                )
            except (ValueError, StopIteration, UnicodeError) as exc:
                raise RippleError("RPL-GIT-005", "Invalid NUL-delimited Git diff output.") from exc
        return tuple(sorted(result, key=lambda f: (f.before_path or "", f.after_path or "")))

    def read_optional(self, tree: str, path: str, limit: int) -> bytes | None:
        entries = run_git(self.root, ["ls-tree", "-l", "-z", tree, "--", path])
        if not entries:
            return None
        metadata, _ = entries.rstrip(b"\0").split(b"\t", 1)
        mode, kind, blob, size = metadata.split()
        if mode == b"120000" or kind != b"blob" or int(size) > limit:
            raise RippleError(
                "RPL-CONFIG-001", "Snapshot config must be a bounded regular blob.", 2
            )
        return run_git(self.root, ["cat-file", "blob", blob.decode("ascii")], limit)
