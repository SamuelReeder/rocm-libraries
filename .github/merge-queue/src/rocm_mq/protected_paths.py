"""Shared RFC §8 protected-path matching helpers.

The handler rejects ``/merge`` before queue routing when a PR touches any path
under ``SELF_BOOTSTRAP_PATHS``. GitHub reports changed file names as strings;
these helpers normalize only spelling variants that can hide the same repo path
(case, slash style, leading ``./`` or ``/``, repeated slashes) and intentionally
do not resolve filesystem state.

Generated-file inventory, 2026-05-24: targeted review of queue-owned workflow,
source, config, and runtime artifact seams found no unprotected source path that
regenerates an output under ``SELF_BOOTSTRAP_PATHS``. ``cycle-summary.md`` is a
runtime artifact emitted from protected queue source/workflow code and is ignored
by ``.github/merge-queue/.gitignore``; it is not an unprotected-source bypass.
"""

from __future__ import annotations

import fnmatch
from typing import Final, Iterable

from rocm_mq.config import SELF_BOOTSTRAP_PATHS

GENERATED_PROTECTED_PATH_PAIRS: Final[tuple[tuple[str, str], ...]] = ()
GENERATED_PROTECTED_PATH_INVENTORY_RESULT: Final[str] = "none found"


def _normalize_for_compare(path: str) -> str:
    """Return a casefolded POSIX-ish spelling for protected-path comparison.

    This is deliberately not path resolution: ``..`` components and symlink
    targets are not interpreted. The changed path string itself is the security
    boundary because GitHub's changed-file API reports repository-relative names.
    """
    normalized = path.replace("\\", "/")
    while normalized.startswith("/") or normalized.startswith("./"):
        if normalized.startswith("/"):
            normalized = normalized[1:]
        else:
            normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized.casefold()


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(
        _normalize_for_compare(path), _normalize_for_compare(pattern)
    )


def is_protected_path(path: str) -> bool:
    """Return whether *path* intersects an RFC §8 self-bootstrap glob."""
    return any(_matches(path, pattern) for pattern in SELF_BOOTSTRAP_PATHS)


def find_protected_path_hits(paths: Iterable[str]) -> list[str]:
    """Return protected changed paths, preserving original strings and order."""
    return [path for path in paths if is_protected_path(path)]


def generated_pair_hits(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """Return source patterns whose inventoried source or output is protected."""
    hits: list[str] = []
    for source, output in pairs:
        if is_protected_path(source) or is_protected_path(output):
            hits.append(source)
    return hits


def find_generated_protected_path_hits(
    paths: Iterable[str],
    pairs: Iterable[tuple[str, str]] = GENERATED_PROTECTED_PATH_PAIRS,
) -> list[str]:
    """Return changed source paths whose generated pair crosses protected roots.

    ``pairs`` contains ``(source_path_or_glob, protected_output_path_or_glob)``
    entries from the explicit generated-file inventory. When an inventoried
    pair's source or output intersects ``SELF_BOOTSTRAP_PATHS``, a change to the
    source pattern is a self-bootstrap hit even if the source path itself is not
    under a protected root. Diagnostic results are the original changed path
    strings, not normalized spellings.
    """
    protected_source_patterns = tuple(generated_pair_hits(pairs))
    if not protected_source_patterns:
        return []
    return [
        path
        for path in paths
        if any(_matches(path, pattern) for pattern in protected_source_patterns)
    ]
