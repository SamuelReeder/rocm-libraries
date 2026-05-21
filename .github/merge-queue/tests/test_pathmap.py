"""
tests/test_pathmap.py — Unit + property tests for rocm_mq.pathmap.queues_for_paths.

Coverage:
- RFC §4.2 membership matrix (seven edge-case rows)
- Asymmetric provider/integration-tests edge (property-tested)
- Longest-prefix-first match correctness
- Empty-path and no-match cases
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rocm_mq.pathmap import queues_for_paths
from rocm_mq.state import AppIdentity, MergeQueueConfig

# ---------------------------------------------------------------------------
# Canonical config factory (RFC §4.2 membership matrix)
# ---------------------------------------------------------------------------

_ALL_QUEUES = (
    "hipdnn",
    "miopen-provider",
    "hipblaslt-provider",
    "hip-kernel-provider",
    "fusilli-provider",
    "integration-tests",
)

# Pre-sorted longest-prefix-first (all prefixes are equal length here,
# but the ordering matches RFC §4.2 rows for deterministic first-match).
_PATH_TO_QUEUES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "projects/hipdnn/",
        frozenset(
            {
                "hipdnn",
                "miopen-provider",
                "hipblaslt-provider",
                "hip-kernel-provider",
                "fusilli-provider",
                "integration-tests",
            }
        ),
    ),
    (
        "dnn-providers/integration-tests/",
        frozenset(
            {
                "miopen-provider",
                "hipblaslt-provider",
                "hip-kernel-provider",
                "fusilli-provider",
                "integration-tests",
            }
        ),
    ),
    (
        "dnn-providers/miopen-provider/",
        frozenset({"miopen-provider"}),
    ),
    (
        "dnn-providers/hipblaslt-provider/",
        frozenset({"hipblaslt-provider"}),
    ),
    (
        "dnn-providers/hip-kernel-provider/",
        frozenset({"hip-kernel-provider"}),
    ),
    (
        "dnn-providers/fusilli-provider/",
        frozenset({"fusilli-provider"}),
    ),
)

_APP_IDENTITY = AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)


def canonical_config() -> MergeQueueConfig:
    """Return a MergeQueueConfig with the RFC §4.2 membership matrix."""
    return MergeQueueConfig(
        all_queues=_ALL_QUEUES,
        path_to_queues=_PATH_TO_QUEUES,
        app_identity=_APP_IDENTITY,
    )


# ---------------------------------------------------------------------------
# Helpers for property tests
# ---------------------------------------------------------------------------

_CANONICAL_OPTED_IN_PREFIXES = [
    "projects/hipdnn/",
    "dnn-providers/integration-tests/",
    "dnn-providers/miopen-provider/",
    "dnn-providers/hipblaslt-provider/",
    "dnn-providers/hip-kernel-provider/",
    "dnn-providers/fusilli-provider/",
]

# One concrete path per prefix for property tests
_CANONICAL_PATHS = [
    "projects/hipdnn/api/foo.h",
    "dnn-providers/integration-tests/foo.py",
    "dnn-providers/miopen-provider/src/x.cpp",
    "dnn-providers/hipblaslt-provider/src/y.cpp",
    "dnn-providers/hip-kernel-provider/src/z.cpp",
    "dnn-providers/fusilli-provider/src/w.cpp",
]

# Per-path expected queue sets (mirrors membership matrix)
_EXPECTED_PER_PATH: dict[str, frozenset[str]] = {
    "projects/hipdnn/api/foo.h": frozenset(
        {
            "hipdnn",
            "miopen-provider",
            "hipblaslt-provider",
            "hip-kernel-provider",
            "fusilli-provider",
            "integration-tests",
        }
    ),
    "dnn-providers/integration-tests/foo.py": frozenset(
        {
            "miopen-provider",
            "hipblaslt-provider",
            "hip-kernel-provider",
            "fusilli-provider",
            "integration-tests",
        }
    ),
    "dnn-providers/miopen-provider/src/x.cpp": frozenset({"miopen-provider"}),
    "dnn-providers/hipblaslt-provider/src/y.cpp": frozenset({"hipblaslt-provider"}),
    "dnn-providers/hip-kernel-provider/src/z.cpp": frozenset({"hip-kernel-provider"}),
    "dnn-providers/fusilli-provider/src/w.cpp": frozenset({"fusilli-provider"}),
}


def _matches_any_prefix(path: str) -> bool:
    """Return True if path matches any opted-in prefix."""
    return any(path.startswith(p) for p in _CANONICAL_OPTED_IN_PREFIXES)


# ---------------------------------------------------------------------------
# Parametrized unit tests — seven-row edge-case table
# ---------------------------------------------------------------------------

_UNIT_TEST_CASES: list[tuple[str, tuple[str, ...], frozenset[str]]] = [
    (
        "hipdnn_only",
        ("projects/hipdnn/api/foo.h",),
        frozenset(
            {
                "hipdnn",
                "miopen-provider",
                "hipblaslt-provider",
                "hip-kernel-provider",
                "fusilli-provider",
                "integration-tests",
            }
        ),
    ),
    (
        "hipdnn_plus_miopen",
        (
            "projects/hipdnn/api/foo.h",
            "dnn-providers/miopen-provider/src/bar.cpp",
        ),
        # core row is a superset — union == all six
        frozenset(
            {
                "hipdnn",
                "miopen-provider",
                "hipblaslt-provider",
                "hip-kernel-provider",
                "fusilli-provider",
                "integration-tests",
            }
        ),
    ),
    (
        "two_providers_no_core",
        (
            "dnn-providers/miopen-provider/x",
            "dnn-providers/fusilli-provider/y",
        ),
        frozenset({"miopen-provider", "fusilli-provider"}),
    ),
    (
        "integration_tests_alone",
        ("dnn-providers/integration-tests/foo.py",),
        # Five queues — NOT hipdnn (asymmetric edge)
        frozenset(
            {
                "miopen-provider",
                "hipblaslt-provider",
                "hip-kernel-provider",
                "fusilli-provider",
                "integration-tests",
            }
        ),
    ),
    (
        "non_opted_in_only",
        ("README.md", "docs/api.md"),
        frozenset(),
    ),
    (
        "empty_paths",
        (),
        frozenset(),
    ),
    (
        "single_provider_miopen",
        ("dnn-providers/miopen-provider/src/x.cpp",),
        frozenset({"miopen-provider"}),
    ),
    (
        "single_provider_hipblaslt",
        ("dnn-providers/hipblaslt-provider/src/y.cpp",),
        frozenset({"hipblaslt-provider"}),
    ),
    (
        "single_provider_hip_kernel",
        ("dnn-providers/hip-kernel-provider/src/z.cpp",),
        frozenset({"hip-kernel-provider"}),
    ),
    (
        "single_provider_fusilli",
        ("dnn-providers/fusilli-provider/src/w.cpp",),
        frozenset({"fusilli-provider"}),
    ),
]


@pytest.mark.parametrize(
    "case_name, paths, expected",
    [(c[0], c[1], c[2]) for c in _UNIT_TEST_CASES],
    ids=[c[0] for c in _UNIT_TEST_CASES],
)
def test_queues_for_paths__unit(
    case_name: str,
    paths: tuple[str, ...],
    expected: frozenset[str],
) -> None:
    """RFC §4.2 membership matrix unit tests (one row per parametrized case)."""
    config = canonical_config()
    result = queues_for_paths(paths, config)
    assert result == expected, (
        f"Case '{case_name}': paths={paths!r}, got {result!r}, expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# Spot-check: asymmetric edge — integration-tests does NOT include hipdnn
# ---------------------------------------------------------------------------


def test_integration_tests_path_does_not_include_hipdnn() -> None:
    """The dnn-providers/integration-tests/ path must NOT include 'hipdnn'."""
    config = canonical_config()
    result = queues_for_paths(("dnn-providers/integration-tests/foo.py",), config)
    assert "hipdnn" not in result
    assert len(result) == 5


def test_hipdnn_path_includes_all_six_queues() -> None:
    """The projects/hipdnn/ path must include all six queues."""
    config = canonical_config()
    result = queues_for_paths(("projects/hipdnn/x",), config)
    assert result == frozenset(_ALL_QUEUES)
    assert len(result) == 6


def test_empty_paths_returns_empty_frozenset() -> None:
    """queues_for_paths((), config) must return frozenset()."""
    config = canonical_config()
    assert queues_for_paths((), config) == frozenset()


# ---------------------------------------------------------------------------
# Property test 1: union of individual path sets
# (for any subset of opted-in paths, result == union of each path's queues)
# ---------------------------------------------------------------------------


@given(st.lists(st.sampled_from(_CANONICAL_PATHS), min_size=0, max_size=10))
@settings(max_examples=200)
def test_property__result_equals_union_of_individual_paths(paths: list[str]) -> None:
    """For any subset of opted-in paths, result == union of each path's queues."""
    config = canonical_config()
    combined = queues_for_paths(tuple(paths), config)
    expected = frozenset().union(*[_EXPECTED_PER_PATH[p] for p in paths])
    assert combined == expected


# ---------------------------------------------------------------------------
# Property test 2: hipdnn path implies all six queues
# ---------------------------------------------------------------------------


@given(
    st.lists(st.sampled_from(_CANONICAL_PATHS), min_size=0, max_size=5),
)
@settings(max_examples=200)
def test_property__hipdnn_path_implies_all_queues(
    other_paths: list[str],
) -> None:
    """Any path list that includes a projects/hipdnn/ entry yields all six queues."""
    hipdnn_path = "projects/hipdnn/api/foo.h"
    paths = (hipdnn_path, *other_paths)
    config = canonical_config()
    result = queues_for_paths(paths, config)
    assert "hipdnn" in result
    assert result == frozenset(_ALL_QUEUES)


# ---------------------------------------------------------------------------
# Property test 3: integration-tests path without hipdnn never includes hipdnn
# ---------------------------------------------------------------------------


@given(
    st.lists(
        st.sampled_from(
            [p for p in _CANONICAL_PATHS if not p.startswith("projects/hipdnn/")]
        ),
        min_size=0,
        max_size=5,
    ),
)
@settings(max_examples=200)
def test_property__integration_tests_without_hipdnn_path(
    other_paths: list[str],
) -> None:
    """integration-tests path + any non-hipdnn paths → result does NOT contain hipdnn."""
    int_tests_path = "dnn-providers/integration-tests/foo.py"
    paths = (int_tests_path, *other_paths)
    config = canonical_config()
    result = queues_for_paths(paths, config)
    assert "hipdnn" not in result
    assert "integration-tests" in result


# ---------------------------------------------------------------------------
# Property test 4: non-opted-in paths → empty frozenset
# ---------------------------------------------------------------------------


@given(
    st.lists(
        st.text(min_size=1).filter(lambda p: not _matches_any_prefix(p)),
        min_size=0,
        max_size=5,
    )
)
@settings(max_examples=200)
def test_property__non_opted_in_paths_return_empty(paths: list[str]) -> None:
    """Any path list that matches no opted-in prefix returns frozenset()."""
    config = canonical_config()
    result = queues_for_paths(tuple(paths), config)
    assert result == frozenset()
