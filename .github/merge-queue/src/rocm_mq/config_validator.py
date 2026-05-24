"""Local validator for .github/merge-queue/path_to_queues.yml.

Runtime queue loading deliberately stays in :mod:`rocm_mq.config` and reads the
configuration from the GitHub Contents API at ``ref=develop``. This module is
for local tests and operator CLI use only: it validates PR-edited YAML before it
can land on ``develop``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from rocm_mq.protected_paths import (
    GENERATED_PROTECTED_PATH_PAIRS,
    find_generated_protected_path_hits,
    is_protected_path,
)

_STAGE_ONE_QUEUES: Final[frozenset[str]] = frozenset(
    {
        "hipdnn",
        "miopen-provider",
        "hipblaslt-provider",
        "hip-kernel-provider",
        "fusilli-provider",
        "integration-tests",
    }
)

_STAGE_ONE_PATH_CLOSURE: Final[dict[str, frozenset[str]]] = {
    "projects/hipdnn/": _STAGE_ONE_QUEUES,
    "dnn-providers/miopen-provider/": frozenset({"miopen-provider"}),
    "dnn-providers/hipblaslt-provider/": frozenset({"hipblaslt-provider"}),
    "dnn-providers/hip-kernel-provider/": frozenset({"hip-kernel-provider"}),
    "dnn-providers/fusilli-provider/": frozenset({"fusilli-provider"}),
    "dnn-providers/integration-tests/": frozenset(
        {
            "miopen-provider",
            "hipblaslt-provider",
            "hip-kernel-provider",
            "fusilli-provider",
            "integration-tests",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class ValidationError:
    """A stable, testable validation diagnostic."""

    code: str
    path: str
    message: str
    details: tuple[str, ...] = ()


def load_local(path: Path) -> dict[str, Any]:
    """Read *path* with ``yaml.safe_load`` and require a mapping root."""
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        msg = f"{path} is not a mapping (got {type(parsed).__name__})"
        raise ValueError(msg)
    return parsed


def validate_path_to_queues(
    payload: Mapping[str, Any],
    *,
    generated_protected_path_pairs: Iterable[tuple[str, str]] = GENERATED_PROTECTED_PATH_PAIRS,
) -> tuple[ValidationError, ...]:
    """Return every semantic error in a ``path_to_queues.yml`` payload.

    The validator intentionally accumulates diagnostics instead of failing fast
    so the existing package test job gives authors all actionable config errors
    in one run.
    """
    errors: list[ValidationError] = []
    queues = _validate_queues(payload, errors)
    path_entries = _validate_paths(payload, errors)

    if queues is not None and path_entries is not None:
        _validate_queue_references(queues, path_entries, errors)
        _validate_stage_one_graph(queues, path_entries, errors)
        _validate_trust_boundaries(
            path_entries,
            generated_protected_path_pairs,
            errors,
        )

    return tuple(errors)


def main(argv: list[str] | None = None) -> int:
    """Local CLI entrypoint for validator tests and operator use."""
    parser = argparse.ArgumentParser(
        prog="rocm-mq validate-config",
        description="Validate .github/merge-queue/path_to_queues.yml locally.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        required=True,
        help="Path to path_to_queues.yml to validate.",
    )
    args = parser.parse_args(argv)

    try:
        payload = load_local(args.path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    errors = validate_path_to_queues(payload)
    for error in errors:
        print(_format_error(error), file=sys.stderr)
    return 0 if not errors else 1


def _validate_queues(
    payload: Mapping[str, Any], errors: list[ValidationError]
) -> tuple[str, ...] | None:
    if "queues" not in payload:
        errors.append(
            ValidationError(
                "shape.missing_queues",
                "queues",
                "top-level queues list is required",
            )
        )
        return None

    raw = payload["queues"]
    if not isinstance(raw, list):
        errors.append(
            ValidationError(
                "shape.queues_not_list",
                "queues",
                "top-level queues must be a list",
                (f"got {type(raw).__name__}",),
            )
        )
        return None

    queues: list[str] = []
    for index, item in enumerate(raw):
        item_path = f"queues[{index}]"
        if not isinstance(item, str):
            errors.append(
                ValidationError(
                    "shape.queue_not_string",
                    item_path,
                    "queue name must be a string",
                    (f"got {type(item).__name__}",),
                )
            )
            continue
        queues.append(item)
    return tuple(queues)


def _validate_paths(
    payload: Mapping[str, Any], errors: list[ValidationError]
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    if "paths" not in payload:
        errors.append(
            ValidationError(
                "shape.missing_paths",
                "paths",
                "top-level paths list is required",
            )
        )
        return None

    raw = payload["paths"]
    if not isinstance(raw, list):
        errors.append(
            ValidationError(
                "shape.paths_not_list",
                "paths",
                "top-level paths must be a list",
                (f"got {type(raw).__name__}",),
            )
        )
        return None

    entries: list[tuple[str, tuple[str, ...]]] = []
    for index, entry in enumerate(raw):
        base_path = f"paths[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(
                ValidationError(
                    "shape.path_entry_not_mapping",
                    base_path,
                    "paths entry must be a mapping",
                    (f"got {type(entry).__name__}",),
                )
            )
            continue

        path = _path_value(entry, base_path, errors)
        path_queues = _entry_queues(entry, base_path, errors)
        if path is not None and path_queues is not None:
            entries.append((path, path_queues))
    return tuple(entries)


def _path_value(
    entry: Mapping[str, Any], base_path: str, errors: list[ValidationError]
) -> str | None:
    if "path" not in entry:
        errors.append(
            ValidationError(
                "shape.path_missing",
                f"{base_path}.path",
                "paths entry must include path",
            )
        )
        return None

    path = entry["path"]
    if not isinstance(path, str):
        errors.append(
            ValidationError(
                "shape.path_not_string",
                f"{base_path}.path",
                "paths entry path must be a string",
                (f"got {type(path).__name__}",),
            )
        )
        return None
    return path


def _entry_queues(
    entry: Mapping[str, Any], base_path: str, errors: list[ValidationError]
) -> tuple[str, ...] | None:
    if "queues" not in entry:
        errors.append(
            ValidationError(
                "shape.entry_queues_missing",
                f"{base_path}.queues",
                "paths entry must include queues",
            )
        )
        return None

    raw = entry["queues"]
    if not isinstance(raw, list):
        errors.append(
            ValidationError(
                "shape.entry_queues_not_list",
                f"{base_path}.queues",
                "paths entry queues must be a list",
                (f"got {type(raw).__name__}",),
            )
        )
        return None

    queues: list[str] = []
    for index, item in enumerate(raw):
        item_path = f"{base_path}.queues[{index}]"
        if not isinstance(item, str):
            errors.append(
                ValidationError(
                    "shape.entry_queue_not_string",
                    item_path,
                    "paths entry queue name must be a string",
                    (f"got {type(item).__name__}",),
                )
            )
            continue
        queues.append(item)
    return tuple(queues)


def _validate_queue_references(
    queues: Sequence[str],
    path_entries: Sequence[tuple[str, Sequence[str]]],
    errors: list[ValidationError],
) -> None:
    declared = set(queues)
    referenced: set[str] = set()
    for path, entry_queues in path_entries:
        for queue in entry_queues:
            referenced.add(queue)
            if queue not in declared:
                errors.append(
                    ValidationError(
                        "ref.unknown_queue",
                        path,
                        "path entry references a queue absent from top-level queues",
                        (queue,),
                    )
                )

    for queue in sorted(declared - referenced):
        errors.append(
            ValidationError(
                "ref.unrouted_queue",
                "queues",
                "declared queue is not referenced by any path entry",
                (queue,),
            )
        )


def _validate_stage_one_graph(
    queues: Sequence[str],
    path_entries: Sequence[tuple[str, Sequence[str]]],
    errors: list[ValidationError],
) -> None:
    queue_set = frozenset(queues)
    if queue_set != _STAGE_ONE_QUEUES:
        errors.append(
            ValidationError(
                "graph.queues_mismatch",
                "queues",
                "top-level queues must match the current Stage-1 six-queue set",
                _sorted_detail("expected", _STAGE_ONE_QUEUES)
                + _sorted_detail("actual", queue_set),
            )
        )

    by_path: dict[str, frozenset[str]] = {
        path: frozenset(entry_queues) for path, entry_queues in path_entries
    }
    actual_paths = frozenset(by_path)
    expected_paths = frozenset(_STAGE_ONE_PATH_CLOSURE)

    for path in sorted(expected_paths - actual_paths):
        errors.append(
            ValidationError(
                "graph.missing_path",
                path,
                "required Stage-1 path entry is missing",
                _sorted_detail("expected_queues", _STAGE_ONE_PATH_CLOSURE[path]),
            )
        )

    for path in sorted(actual_paths - expected_paths):
        errors.append(
            ValidationError(
                "graph.unexpected_path",
                path,
                "path entry is outside the current exact Stage-1 graph",
            )
        )

    for path in sorted(expected_paths & actual_paths):
        expected_queues = _STAGE_ONE_PATH_CLOSURE[path]
        actual_queues = by_path[path]
        if actual_queues != expected_queues:
            errors.append(
                ValidationError(
                    "graph.path_queues_mismatch",
                    path,
                    "path entry queues do not match Stage-1 graph closure",
                    _sorted_detail("expected", expected_queues)
                    + _sorted_detail("actual", actual_queues),
                )
            )


def _validate_trust_boundaries(
    path_entries: Sequence[tuple[str, Sequence[str]]],
    generated_pairs: Iterable[tuple[str, str]],
    errors: list[ValidationError],
) -> None:
    paths = tuple(path for path, _ in path_entries)
    generated_hits = frozenset(
        find_generated_protected_path_hits(paths, tuple(generated_pairs))
    )
    for path in paths:
        if is_protected_path(path) or _is_github_root_route(path):
            errors.append(
                ValidationError(
                    "trust.protected_path",
                    path,
                    "queue route path intersects RFC §8 protected roots",
                )
            )
        if path in generated_hits:
            errors.append(
                ValidationError(
                    "trust.generated_protected_path",
                    path,
                    "queue route path is an inventoried source for protected generated output",
                )
            )

def _is_github_root_route(path: str) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("/") or normalized.startswith("./"):
        if normalized.startswith("/"):
            normalized = normalized[1:]
        else:
            normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized.casefold().startswith(".github/")

def _sorted_detail(label: str, values: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"{label}={value}" for value in sorted(values))


def _format_error(error: ValidationError) -> str:
    details = "" if not error.details else " " + " ".join(error.details)
    return f"{error.code}: {error.path}: {error.message}{details}"


__all__ = ["ValidationError", "load_local", "main", "validate_path_to_queues"]
