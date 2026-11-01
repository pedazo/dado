"""Cached JSON Schema validation with clear path and field diagnostics.

The Draft 2020-12 validators (and their schema documents) are compiled once
per ``(kind, schema path)`` pair and then reused, so hot paths such as task
packet validation no longer re-parse the schema JSON on every call.

Diagnostics are rendered as ``<location>: <message>`` items where the
location is the JSON path of the offending value inside the document, e.g.
``depends_on[0]: 'T-01' does not match '^T-[0-9]{3,}$'``. Locations are
stringified before sorting so multiple errors can never compare a string
path against an integer index (which previously raised ``TypeError`` in the
middle of error collection, turning a plain schema error into a traceback).
"""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ValidationError

_MAX_REPORTED_ERRORS = 5

_VALIDATORS: dict[tuple[str, str], Draft202012Validator] = {}
_CACHE_LOCK = Lock()


def _schema_document(kind: str, schema_path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Cannot load the {kind} schema from {schema_path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValidationError(f"The {kind} schema at {schema_path} must be a JSON object")
    return schema


def load_validator(kind: str, schema_path: Path) -> Draft202012Validator:
    """Return the cached ``Draft202012Validator`` for one ``(kind, schema file)``.

    Schema files are read-only package data, so a validator compiled once
    stays valid for the life of the process without any staleness check.
    """
    key = (kind, str(schema_path))
    with _CACHE_LOCK:
        validator = _VALIDATORS.get(key)
    if validator is None:
        validator = Draft202012Validator(_schema_document(kind, schema_path))
        with _CACHE_LOCK:
            # setdefault keeps the first compiled validator when threads race;
            # the loser is garbage collected without ever being handed out twice.
            validator = _VALIDATORS.setdefault(key, validator)
    return validator


def clear_cache() -> None:
    """Drop every cached validator (used by tests)."""
    with _CACHE_LOCK:
        _VALIDATORS.clear()


def _location(error: Any) -> str:
    """Render the JSON path of one jsonschema error, e.g. ``scope.allowed[0]``."""
    parts: list[str] = []
    for part in error.absolute_path:
        if isinstance(part, int):
            if parts:
                parts[-1] += f"[{part}]"
            else:
                parts.append(f"item[{part}]")
        else:
            parts.append(str(part))
    if not parts:
        return "<document>"
    return ".".join(parts)


def diagnostics(kind: str, data: Any, schema_path: Path) -> None:
    """Validate ``data`` against ``kind``'s cached schema.

    Raises :class:`ValidationError` whose message names the offending field
    and its location inside the document. Validatees of any shape (scalars,
    lists, ``None``) are handled: schema errors are reported, never raised
    as raw tracebacks.
    """
    validator = load_validator(kind, schema_path)
    errors = sorted(validator.iter_errors(data), key=_location)
    if not errors:
        return
    details = "; ".join(f"{_location(error)}: {error.message}" for error in errors[:_MAX_REPORTED_ERRORS])
    raise ValidationError(f"Invalid {kind}: {details}")
