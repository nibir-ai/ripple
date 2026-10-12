"""Owned per-workspace SQLite cache with transactional publication and JSON payloads."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ripple.cache_codec import MAX_PAYLOAD, ast_data, checksum, read_ast, read_snapshot, typed_value
from ripple.models import Report, Snapshot, stable_id

SCHEMA_VERSION = 3
APPLICATION_ID = 0x52504C45
ANALYZER = "python-static-cache-v1"
SYNTAX_VERSION = "python-ast-scope-v1"


class CacheUnavailable(Exception):
    """Cache failure may fall back to a full index; it is not a semantic diagnostic."""


def runtime_key() -> str:
    return (
        f"{sys.implementation.name}-"
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )


def default_cache_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Ripple"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ripple"


def workspace_id(root: Path) -> str:
    return checksum(os.path.normcase(str(root.resolve())))


def check_path(path: Path) -> None:
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            stat = component.lstat()
            if component.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                raise CacheUnavailable("Cache paths cannot contain symlinks or reparse points.")


def snapshot_key(identity: str, tree: str, config: str, limits: str) -> str:
    return stable_id(identity, tree, config, ANALYZER, SYNTAX_VERSION, runtime_key(), limits)


class Cache:
    def __init__(self, root: Path, base: Path | None = None, *, repair: bool = True):
        self.identity = workspace_id(root)
        self.directory = (base or default_cache_dir()).absolute() / self.identity
        self.path = self.directory / "index.sqlite3"
        self.marker = self.directory / "owner.json"
        self.repaired: str | None = None
        check_path(self.directory)
        if not self.directory.exists() and not repair:
            raise CacheUnavailable("Cache does not exist; run ripple index.")
        if repair:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        check_path(self.path)
        check_path(self.marker)
        if self.marker.exists():
            if self.marker.stat().st_size > 4096:
                raise CacheUnavailable("Invalid cache ownership marker.")
            try:
                owner = json.loads(self.marker.read_text())
            except (ValueError, OSError) as exc:
                raise CacheUnavailable("Unreadable cache ownership marker.") from exc
            if owner != {"format": "ripple-cache-v1", "workspace": self.identity}:
                raise CacheUnavailable("Cache ownership does not match this workspace.")
        elif self.path.exists() or not repair:
            raise CacheUnavailable("Refusing an unowned cache database.")
        else:
            try:
                with self.marker.open("x", encoding="utf-8") as handle:
                    json.dump({"format": "ripple-cache-v1", "workspace": self.identity}, handle)
                if os.name != "nt":
                    self.marker.chmod(0o600)
            except FileExistsError:
                # A concurrent creator owns the same deterministic workspace directory.
                owner = json.loads(self.marker.read_text())
                if owner.get("workspace") != self.identity:
                    raise CacheUnavailable("Concurrent cache ownership mismatch.") from None
        try:
            self._initialize(migrate=repair)
        except (sqlite3.DatabaseError, ValueError) as exc:
            if isinstance(exc, sqlite3.OperationalError) and any(
                token in str(exc).lower() for token in ("locked", "busy", "readonly", "disk")
            ):
                raise CacheUnavailable("Cache unavailable or busy; use a full index.") from exc
            if not repair:
                raise CacheUnavailable(
                    "Cache corrupt or schema incompatible; rerun index to repair."
                ) from exc
            # Preserve the owned broken database rather than delete arbitrary files.
            check_path(self.path)
            recovery = self.directory / "recovery.lock"
            try:
                recovery.mkdir()
            except FileExistsError as locked:
                raise CacheUnavailable("Cache recovery is already in progress.") from locked
            try:
                backup = self.path.with_name("index.sqlite3.quarantine." + uuid.uuid4().hex)
                self.path.replace(backup)
                journal = self.path.with_name("index.sqlite3-journal")
                check_path(journal)
                if journal.exists():
                    journal.replace(backup.with_name(backup.name + "-journal"))
                self.repaired = "Corrupt/incompatible cache quarantined; rebuilding."
                self._initialize(migrate=True)
            finally:
                recovery.rmdir()

    @contextmanager
    def connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        check_path(self.path)
        uri = self.path.as_uri() + "?mode=ro" if readonly else str(self.path)
        try:
            connection = sqlite3.connect(uri, uri=readonly, timeout=1.0)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_PAYLOAD)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA busy_timeout=1000")
            try:
                yield connection
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise CacheUnavailable(
                f"SQLite cache failure ({type(exc).__name__}); full index required."
            ) from exc

    def _initialize(self, *, migrate: bool) -> None:
        # This connection is deliberately separate so constructor recovery sees SQLite errors.
        connection = sqlite3.connect(
            str(self.path) if migrate else self.path.as_uri() + "?mode=ro",
            uri=not migrate,
            timeout=1.0,
        )
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            if version > SCHEMA_VERSION or version < 0 or application not in (0, APPLICATION_ID):
                raise ValueError("Incompatible cache schema")
            if version and application != APPLICATION_ID:
                raise ValueError("Unknown database owner")
            if not migrate and version != SCHEMA_VERSION:
                raise ValueError("Migration required")
            if migrate and version < SCHEMA_VERSION:
                connection.execute("BEGIN IMMEDIATE")
                if version == 0:
                    if connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchone():
                        raise ValueError("Unknown schema")
                    statements = (
                        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, "
                        "applied_at REAL NOT NULL)",
                        "CREATE TABLE snapshots(key TEXT PRIMARY KEY, tree TEXT NOT NULL, "
                        "config TEXT NOT NULL, "
                        "runtime TEXT NOT NULL, analyzer TEXT NOT NULL, status TEXT NOT NULL "
                        "CHECK(status IN ('staging','complete')), created_at REAL NOT NULL, "
                        "payload TEXT NOT NULL, checksum TEXT NOT NULL)",
                        "CREATE TABLE symbols(snapshot TEXT NOT NULL REFERENCES "
                        "snapshots(key) ON DELETE CASCADE, "
                        "id TEXT NOT NULL, path TEXT NOT NULL, payload TEXT NOT NULL, "
                        "PRIMARY KEY(snapshot,id))",
                        "CREATE TABLE reference_sites(snapshot TEXT NOT NULL, id TEXT NOT "
                        "NULL, source TEXT NOT NULL, "
                        "target TEXT, state TEXT NOT NULL CHECK(state IN "
                        "('resolved','candidate','unresolved','external','excluded')), "
                        "payload TEXT NOT NULL, PRIMARY KEY(snapshot,id), "
                        "FOREIGN KEY(snapshot,source) REFERENCES symbols(snapshot,id), "
                        "FOREIGN KEY(snapshot,target) REFERENCES symbols(snapshot,id), "
                        "FOREIGN KEY(snapshot) REFERENCES snapshots(key) ON DELETE CASCADE)",
                        "CREATE TABLE edges(snapshot TEXT NOT NULL, ordinal INTEGER NOT "
                        "NULL, source TEXT NOT NULL, "
                        "target TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, "
                        "PRIMARY KEY(snapshot,ordinal), "
                        "FOREIGN KEY(snapshot,source) REFERENCES symbols(snapshot,id), "
                        "FOREIGN KEY(snapshot,target) REFERENCES symbols(snapshot,id), "
                        "FOREIGN KEY(snapshot) REFERENCES snapshots(key) ON DELETE CASCADE)",
                        "CREATE TABLE diagnostics(snapshot TEXT NOT NULL REFERENCES "
                        "snapshots(key) ON DELETE CASCADE, "
                        "ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY "
                        "KEY(snapshot,ordinal))",
                        "CREATE INDEX ix_edges_incoming ON edges(snapshot,kind,target)",
                        "CREATE INDEX ix_edges_outgoing ON edges(snapshot,kind,source)",
                        "CREATE INDEX ix_symbols_file ON symbols(snapshot,path)",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations VALUES(1,?)", (time.time(),))
                    version = 1
                if version == 1:
                    connection.execute(
                        "CREATE TABLE syntax(key TEXT PRIMARY KEY, blob TEXT NOT NULL, "
                        "runtime TEXT NOT NULL, "
                        "source BLOB NOT NULL, ast TEXT NOT NULL, checksum TEXT NOT NULL)"
                    )
                    connection.execute("INSERT INTO schema_migrations VALUES(2,?)", (time.time(),))
                    version = 2
                if version == 2:
                    connection.execute(
                        "CREATE TABLE analysis_runs(key TEXT PRIMARY KEY, "
                        "base_key TEXT NOT NULL REFERENCES snapshots(key) ON DELETE CASCADE, "
                        "head_key TEXT NOT NULL REFERENCES snapshots(key) ON DELETE CASCADE, "
                        "policy TEXT NOT NULL, created_at REAL NOT NULL, "
                        "payload TEXT NOT NULL, checksum TEXT NOT NULL)"
                    )
                    connection.execute("INSERT INTO schema_migrations VALUES(3,?)", (time.time(),))
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.commit()
            required = {
                "schema_migrations",
                "snapshots",
                "symbols",
                "reference_sites",
                "edges",
                "diagnostics",
                "syntax",
                "analysis_runs",
            }
            present = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not required <= present:
                raise ValueError("Missing cache tables")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Corrupt cache")
            if os.name != "nt" and migrate:
                self.path.chmod(0o600)
        finally:
            connection.close()

    def get_snapshot(self, key: str, tree: str, config: str) -> Snapshot | None:
        with self.connection(readonly=True) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT payload,checksum FROM snapshots WHERE key=? AND status='complete' "
                "AND tree=? AND config=? AND runtime=? AND analyzer=?",
                (key, tree, config, runtime_key(), ANALYZER),
            ).fetchone()
            counts = (
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE snapshot=?", (key,)
                    ).fetchone()[0]
                    for table in ("symbols", "reference_sites", "edges", "diagnostics")
                )
                if row is not None
                else ()
            )
        if row is None:
            return None
        try:
            if checksum(row[0]) != row[1]:
                raise ValueError("Snapshot checksum mismatch")
            snapshot = read_snapshot(row[0])
            if snapshot.tree != tree or snapshot.config_fingerprint != config:
                raise ValueError("Snapshot identity mismatch")
            if counts != (
                len(snapshot.symbols),
                len(snapshot.references),
                len(snapshot.edges),
                len(snapshot.diagnostics),
            ):
                raise ValueError("Graph inventory mismatch")
            return snapshot
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CacheUnavailable("Invalid cached snapshot payload; force a rebuild.") from exc

    def get_syntax(self, blob: str, max_nodes: int) -> tuple[bytes, ast.Module] | None:
        key = stable_id(SYNTAX_VERSION, runtime_key(), blob, str(max_nodes))
        with self.connection(readonly=True) as connection:
            row = connection.execute(
                "SELECT source,ast,checksum FROM syntax WHERE key=? AND blob=? AND runtime=?",
                (key, blob, runtime_key()),
            ).fetchone()
        if row is None:
            return None
        try:
            if len(row[0]) > 2 * 1024 * 1024 or checksum(row[0] + row[1].encode()) != row[2]:
                raise ValueError("Syntax checksum mismatch")
            hasher = hashlib.sha1 if len(blob) == 40 else hashlib.sha256
            if hasher(f"blob {len(row[0])}\0".encode() + row[0]).hexdigest() != blob:
                raise ValueError("Cached source does not match the Git blob")
            return bytes(row[0]), read_ast(row[1], max_nodes)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CacheUnavailable("Invalid cached syntax; reparse required.") from exc

    def publish(
        self,
        key: str,
        snapshot: Snapshot,
        syntax: list[tuple[str, bytes, ast.Module]],
        max_nodes: int,
    ) -> None:
        payload = json.dumps(asdict(snapshot), ensure_ascii=True, sort_keys=True)
        if len(payload) > MAX_PAYLOAD:
            raise CacheUnavailable("Snapshot too large to cache; full result remains available.")
        read_snapshot(payload)
        with self.connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,'staging',?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET tree=excluded.tree,config=excluded.config, "
                "runtime=excluded.runtime,analyzer=excluded.analyzer,status='staging', "
                "created_at=excluded.created_at,payload=excluded.payload,checksum=excluded.checksum",
                (
                    key,
                    snapshot.tree,
                    snapshot.config_fingerprint,
                    runtime_key(),
                    ANALYZER,
                    time.time(),
                    payload,
                    checksum(payload),
                ),
            )
            for table in ("edges", "reference_sites", "diagnostics", "symbols"):
                connection.execute(f"DELETE FROM {table} WHERE snapshot=?", (key,))
            for symbol in snapshot.symbols:
                connection.execute(
                    "INSERT INTO symbols VALUES(?,?,?,?)",
                    (key, symbol.id, symbol.location.path, json.dumps(asdict(symbol))),
                )
            for ref in snapshot.references:
                connection.execute(
                    "INSERT INTO reference_sites VALUES(?,?,?,?,?,?)",
                    (
                        key,
                        ref.id,
                        ref.source_id,
                        ref.target_id,
                        ref.resolution_state,
                        json.dumps(asdict(ref)),
                    ),
                )
            for i, edge in enumerate(snapshot.edges):
                connection.execute(
                    "INSERT INTO edges VALUES(?,?,?,?,?,?)",
                    (
                        key,
                        i,
                        edge.source_id,
                        edge.target_id,
                        edge.edge_type,
                        json.dumps(asdict(edge)),
                    ),
                )
            for i, diagnostic in enumerate(snapshot.diagnostics):
                connection.execute(
                    "INSERT INTO diagnostics VALUES(?,?,?)",
                    (key, i, json.dumps(asdict(diagnostic))),
                )
            for blob, source, parsed in syntax:
                encoded = json.dumps(ast_data(parsed), ensure_ascii=True, sort_keys=True)
                if len(encoded) > MAX_PAYLOAD:
                    continue
                syntax_key = stable_id(SYNTAX_VERSION, runtime_key(), blob, str(max_nodes))
                connection.execute(
                    "INSERT OR REPLACE INTO syntax VALUES(?,?,?,?,?,?)",
                    (
                        syntax_key,
                        blob,
                        runtime_key(),
                        source,
                        encoded,
                        checksum(source + encoded.encode()),
                    ),
                )
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise CacheUnavailable("Snapshot referential integrity check failed.")
            connection.execute("UPDATE snapshots SET status='complete' WHERE key=?", (key,))

    def status(self) -> dict[str, object]:
        with self.connection(readonly=True) as connection:
            connection.execute("BEGIN")
            snapshots = connection.execute(
                "SELECT tree,config,runtime,analyzer,created_at,payload,key FROM snapshots "
                "WHERE status='complete' ORDER BY created_at DESC"
            ).fetchall()
            syntax_count = connection.execute("SELECT COUNT(*) FROM syntax").fetchone()[0]
            last = connection.execute(
                "SELECT head_key,created_at,payload FROM analysis_runs "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        last_report = json.loads(last[2]) if last else None
        return {
            "cache_path": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "size_bytes": self.path.stat().st_size,
            "snapshot_count": len(snapshots),
            "syntax_count": syntax_count,
            "last_analysis": {
                "head_key": last[0],
                "created_at": last[1],
                "comparison": last_report["comparison"],
                "policy": last_report["policy"],
                "completeness": last_report["completeness"]["status"],
            }
            if last and last_report
            else None,
            "snapshots": [
                {
                    "key": r[6],
                    "tree": r[0],
                    "config": r[1],
                    "runtime": r[2],
                    "analyzer": r[3],
                    "created_at": r[4],
                    "source_roots": json.loads(r[5])["source_roots"],
                    "compatible": r[2] == runtime_key() and r[3] == ANALYZER,
                }
                for r in snapshots
            ],
            "quarantined_databases": sum(
                not p.name.endswith("-journal")
                for p in self.directory.glob("index.sqlite3.quarantine.*")
            ),
        }

    def doctor(self) -> dict[str, object]:
        with self.connection(readonly=True) as connection:
            connection.execute("BEGIN")
            integrity = [r[0] for r in connection.execute("PRAGMA integrity_check")]
            foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
            rows = connection.execute("SELECT key,tree,config,status FROM snapshots").fetchall()
            syntax_rows = connection.execute(
                "SELECT runtime,source,ast,checksum FROM syntax"
            ).fetchall()
            reports = connection.execute("SELECT payload,checksum FROM analysis_runs").fetchall()
        invalid = 0
        for key, tree, config, status in rows:
            try:
                if status != "complete":
                    raise ValueError("Invalid snapshot")
                self.get_snapshot(key, tree, config)
            except (CacheUnavailable, ValueError, TypeError, KeyError, RecursionError):
                invalid += 1
        invalid_syntax = 0
        for runtime, source, parsed, digest in syntax_rows:
            try:
                if checksum(source + parsed.encode()) != digest:
                    raise ValueError("Syntax checksum mismatch")
                if runtime == runtime_key():
                    read_ast(parsed, 100000)
            except (ValueError, TypeError, KeyError, RecursionError):
                invalid_syntax += 1
        invalid_reports = 0
        for payload, digest in reports:
            try:
                if checksum(payload) != digest:
                    raise ValueError("Report checksum mismatch")
                typed_value(json.loads(payload), Report)
            except (ValueError, TypeError, KeyError, RecursionError):
                invalid_reports += 1
        return {
            "healthy": integrity == ["ok"]
            and not foreign_keys
            and not invalid
            and not invalid_syntax
            and not invalid_reports,
            "integrity": integrity,
            "foreign_key_errors": len(foreign_keys),
            "invalid_snapshots": invalid,
            "invalid_syntax_records": invalid_syntax,
            "invalid_reports": invalid_reports,
            "advice": "Use index --force to rebuild invalid snapshots; "
            "incompatible schemas are quarantined.",
        }

    def save_report(self, report: Report, base_key: str, head_key: str) -> None:
        payload = report.to_json()
        if len(payload) > MAX_PAYLOAD:
            raise CacheUnavailable("Report too large to persist; output remains available.")
        policy = stable_id(
            json.dumps(asdict(report.policy), sort_keys=True),
            report.tool["rename_threshold"],
            report.tool["version"],
        )
        key = stable_id(base_key, head_key, policy)
        with self.connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO analysis_runs VALUES(?,?,?,?,?,?,?)",
                (key, base_key, head_key, policy, time.time(), payload, checksum(payload)),
            )

    def clear(self) -> None:
        # Clear only rows in this owned workspace database, never source/cache directories.
        with self.connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for table in (
                "analysis_runs",
                "edges",
                "reference_sites",
                "diagnostics",
                "symbols",
                "snapshots",
                "syntax",
            ):
                connection.execute(f"DELETE FROM {table}")
        with self.connection() as connection:
            connection.execute("VACUUM")
