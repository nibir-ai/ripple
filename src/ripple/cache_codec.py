"""Bounded JSON codecs for cache data; never pickle or execute serialized objects."""

from __future__ import annotations

import ast
import hashlib
import json
import types
from dataclasses import fields, is_dataclass
from functools import cache
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from ripple.models import Snapshot

MAX_PAYLOAD = 64 * 1024 * 1024


def checksum(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


@cache
def record_types(annotation: type[Any]) -> tuple[set[str], dict[str, Any]]:
    return {f.name for f in fields(annotation)}, get_type_hints(annotation)


def typed_value(value: Any, annotation: Any) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        for option in args:
            try:
                return typed_value(value, option)
            except (TypeError, ValueError):
                pass
        raise ValueError("Invalid union field")
    if annotation is type(None):
        if value is not None:
            raise ValueError("Expected null")
        return None
    if origin is Literal:
        if value not in args:
            raise ValueError("Invalid literal")
        return value
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError("Expected array")
        return tuple(typed_value(v, args[0]) for v in value)
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("Expected mapping")
        return {typed_value(k, args[0]): typed_value(v, args[1]) for k, v in value.items()}
    if isinstance(annotation, type) and is_dataclass(annotation):
        names, hints = record_types(annotation)
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError("Invalid record fields")
        return annotation(**{k: typed_value(v, hints[k]) for k, v in value.items()})
    if annotation in (str, int, bool):
        if type(value) is not annotation:
            raise ValueError("Invalid primitive")
        return value
    raise ValueError("Unsupported cache type")


def read_snapshot(payload: str) -> Snapshot:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Snapshot payload too large")
    result: Snapshot = typed_value(json.loads(payload), Snapshot)
    symbols = {s.id for s in result.symbols}
    if len(symbols) != len(result.symbols) or result.parsed_files < 0 or result.skipped_files < 0:
        raise ValueError("Invalid snapshot inventory")
    if len({r.id for r in result.references}) != len(result.references):
        raise ValueError("Duplicate reference")
    if any(
        r.source_id not in symbols or (r.target_id and r.target_id not in symbols)
        for r in result.references
    ):
        raise ValueError("Dangling reference")
    if any(e.source_id not in symbols or e.target_id not in symbols for e in result.edges):
        raise ValueError("Dangling edge")
    return result


def ast_data(value: Any) -> Any:
    if isinstance(value, ast.AST):
        return {
            "node": type(value).__name__,
            "fields": {name: ast_data(v) for name, v in ast.iter_fields(value)},
            "attrs": {
                name: getattr(value, name) for name in value._attributes if hasattr(value, name)
            },
        }
    if isinstance(value, list):
        return [ast_data(v) for v in value]
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, complex):
        return {"complex": [value.real, value.imag]}
    if value is Ellipsis:
        return {"ellipsis": True}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("Unsupported AST value")


def read_ast(payload: str, max_nodes: int) -> ast.Module:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Syntax payload too large")
    count = 0

    def decode(value: Any) -> Any:
        nonlocal count
        if isinstance(value, list):
            return [decode(v) for v in value]
        if not isinstance(value, dict):
            if value is None or type(value) in (str, int, float, bool):
                return value
            raise ValueError("Invalid syntax value")
        if set(value) == {"bytes"} and isinstance(value["bytes"], str):
            return bytes.fromhex(value["bytes"])
        if set(value) == {"complex"} and isinstance(value["complex"], list):
            return complex(*value["complex"])
        if value == {"ellipsis": True}:
            return Ellipsis
        if set(value) != {"node", "fields", "attrs"}:
            raise ValueError("Invalid syntax record")
        cls = getattr(ast, value["node"], None) if isinstance(value["node"], str) else None
        if not isinstance(cls, type) or not issubclass(cls, ast.AST):
            raise ValueError("Unknown AST node")
        count += 1
        if count > max_nodes:
            raise ValueError("Syntax node limit")
        field_data, attributes = value["fields"], value["attrs"]
        if not isinstance(field_data, dict) or set(field_data) != set(cls._fields):
            raise ValueError("Invalid AST fields")
        if not isinstance(attributes, dict) or not set(attributes) <= set(cls._attributes):
            raise ValueError("Invalid AST attributes")
        if any(v is not None and (type(v) is not int or v < 0) for v in attributes.values()):
            raise ValueError("Invalid syntax coordinates")
        return cls(**{k: decode(v) for k, v in field_data.items()}, **attributes)

    result = decode(json.loads(payload))
    if not isinstance(result, ast.Module):
        raise ValueError("Expected module AST")
    return result
