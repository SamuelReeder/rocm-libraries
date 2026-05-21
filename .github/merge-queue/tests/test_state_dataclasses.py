"""
test_state_dataclasses.py — Invariant tests for state.py frozen dataclasses.

PURE-01: Every dataclass exported from state.py is:
  1. frozen=True (immutable; auto-generates __hash__ + __eq__)
  2. slots=True (__slots__ defined — catches typo-introduced attribute writes)
  3. Uses no mutable container types (no list[...], set[...], dict[...] field annotations)
  4. Any datetime-annotated field should be tz-aware (documented contract + guard helper)

Also includes:
  - Anti-Pydantic canary (Pitfall 1): pydantic must not be in sys.modules after import.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
from datetime import UTC, datetime

import pytest

import rocm_mq.state as state_module

# ---------------------------------------------------------------------------
# Collect all dataclasses exported from state.py
# ---------------------------------------------------------------------------


def _get_state_dataclasses() -> list[type]:
    """Return all dataclass types exported from rocm_mq.state."""
    return [
        obj
        for _, obj in inspect.getmembers(state_module, inspect.isclass)
        if dataclasses.is_dataclass(obj) and obj.__module__ == "rocm_mq.state"
    ]


STATE_DATACLASSES = _get_state_dataclasses()
STATE_DATACLASS_NAMES = [c.__name__ for c in STATE_DATACLASSES]


# ---------------------------------------------------------------------------
# Test 1: Every dataclass is frozen=True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", STATE_DATACLASSES, ids=STATE_DATACLASS_NAMES)
def test_dataclass_is_frozen(cls: type) -> None:
    """Every dataclass in state.py must have frozen=True."""
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen is True, (
        f"{cls.__name__} is not frozen — add frozen=True to @dataclass()"
    )


# ---------------------------------------------------------------------------
# Test 2: Every dataclass has __slots__ defined
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", STATE_DATACLASSES, ids=STATE_DATACLASS_NAMES)
def test_dataclass_has_slots(cls: type) -> None:
    """Every dataclass in state.py must have slots=True (__slots__ in cls.__dict__)."""
    assert "__slots__" in cls.__dict__, (
        f"{cls.__name__} is missing __slots__ — add slots=True to @dataclass()"
    )


# ---------------------------------------------------------------------------
# Test 3: No mutable container type annotations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", STATE_DATACLASSES, ids=STATE_DATACLASS_NAMES)
def test_no_mutable_container_annotations(cls: type) -> None:
    """No dataclass field may be annotated with list[...], set[...], or dict[...]."""
    mutable_violations = []
    for field in dataclasses.fields(cls):
        annotation_str = str(field.type)
        # Check annotation string for mutable container types
        # This is greenfield code — string matching is sufficient and correct
        if any(f": {mut}" in f" {annotation_str}" or annotation_str.startswith(mut)
               for mut in ("list[", "set[", "dict[")):
            mutable_violations.append(field.name)
        # Also check for bare annotation strings that start with mutable types
        for mutable_prefix in ("list", "set", "dict"):
            if (annotation_str == mutable_prefix or annotation_str.startswith(
                f"{mutable_prefix}["
            )) and field.name not in mutable_violations:
                mutable_violations.append(field.name)

    assert not mutable_violations, (
        f"{cls.__name__} has mutable container annotations on fields: "
        f"{mutable_violations}. Use tuple[T, ...] or frozenset[T] instead."
    )


# ---------------------------------------------------------------------------
# Test 4: datetime fields contract — every datetime must be tz-aware
# (guard helper + documentation test)
# ---------------------------------------------------------------------------


def assert_all_datetimes_tz_aware(instance: object) -> list[str]:
    """Check all datetime fields on an instance for tz-awareness.

    Returns a list of field names where tzinfo is None (naive datetime).
    Empty list means all datetimes are tz-aware.
    """
    violations = []
    if not dataclasses.is_dataclass(instance):
        return violations
    for field in dataclasses.fields(instance):  # type: ignore[arg-type]
        value = getattr(instance, field.name)
        if isinstance(value, datetime) and value.tzinfo is None:
            violations.append(field.name)
    return violations


def test_assert_all_datetimes_tz_aware_helper_catches_naive() -> None:
    """The guard helper correctly identifies naive datetime fields."""

    from rocm_mq.state import CommitStatus, CommitStatusCreator

    creator = CommitStatusCreator(
        login="test-bot",
        type="Bot",
        app_slug="test-app",
        app_id=1,
    )
    # Construct a CommitStatus with a naive datetime (no tzinfo)
    naive_dt = datetime(2026, 4, 22, 15, 23, 45)  # no tzinfo
    status_naive = CommitStatus(
        context="merge-queue/active",
        state="success",
        creator=creator,
        created_at=naive_dt,
    )
    violations = assert_all_datetimes_tz_aware(status_naive)
    assert "created_at" in violations, (
        "Guard helper should have caught naive datetime on created_at"
    )

    # Verify a tz-aware datetime passes the guard
    aware_dt = datetime(2026, 4, 22, 15, 23, 45, tzinfo=UTC)
    status_aware = CommitStatus(
        context="merge-queue/active",
        state="success",
        creator=creator,
        created_at=aware_dt,
    )
    violations_aware = assert_all_datetimes_tz_aware(status_aware)
    assert violations_aware == [], (
        f"Guard helper should NOT flag tz-aware datetime: {violations_aware}"
    )


def test_all_datetime_fields_in_dataclasses_are_documented_as_tz_required() -> None:
    """Enumerate all datetime fields across state.py dataclasses for documentation."""
    datetime_fields: dict[str, list[str]] = {}
    for cls in STATE_DATACLASSES:
        for field in dataclasses.fields(cls):
            annotation_str = str(field.type)
            if "datetime" in annotation_str:
                datetime_fields.setdefault(cls.__name__, []).append(field.name)

    # The following dataclasses have datetime fields (must be tz-aware by contract):
    expected_datetime_types = {
        "CommitStatus": ["created_at"],
        "LabelEvent": ["created_at"],
        "PRState": ["enqueued_at"],
        "CycleRenderContext": ["cycle_started_at", "cycle_completed_at"],
    }

    for cls_name, fields in expected_datetime_types.items():
        assert cls_name in datetime_fields, (
            f"{cls_name} expected to have datetime fields {fields} but was not found"
        )
        for field_name in fields:
            assert field_name in datetime_fields[cls_name], (
                f"{cls_name}.{field_name} expected to be a datetime field"
            )


# ---------------------------------------------------------------------------
# Anti-Pydantic canary (Pitfall 1)
# ---------------------------------------------------------------------------


def test_anti_pydantic_canary_no_basemodel_in_state() -> None:
    """state.py must not export BaseModel (or any Pydantic type)."""
    assert not hasattr(state_module, "BaseModel"), (
        "rocm_mq.state exports a Pydantic BaseModel — this violates the "
        "pure-dataclass contract (STACK.md 'What NOT to Use')"
    )


def test_anti_pydantic_canary_pydantic_not_imported() -> None:
    """pydantic must not be imported as a side-effect of importing rocm_mq.

    Run in a clean subprocess so the check is not polluted by other tests in
    this session that legitimately import ``rocm_mq.gh`` (which transitively
    pulls in githubkit -> pydantic). The pure-layer contract is that importing
    ``rocm_mq`` itself — the public API surface — does NOT drag pydantic
    into sys.modules; importing the I/O sibling ``rocm_mq.gh`` is allowed to.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import rocm_mq; "
            "assert 'pydantic' not in sys.modules, "
            "'pydantic in sys.modules after import rocm_mq'",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "pydantic was found in sys.modules after importing rocm_mq in a clean "
        "subprocess — this violates the pure-dataclass contract "
        f"(Pitfall 1, STACK.md). stderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Completeness check: expected dataclasses are all present
# ---------------------------------------------------------------------------


_EXPECTED_DATACLASS_NAMES = {
    # Raw family
    "CommitStatusCreator",
    "CommitStatus",
    "TimelineActor",
    "LabelEvent",
    "RawPRState",
    "RawSnapshot",
    # Derived family
    "PRState",
    "Snapshot",
    # Q1/Q4 sum types
    "DeferredPR",
    "PartialPRState",
    # Action variants
    "Activate",
    "Squash",
    "Eject",
    "UpdateComment",
    "Defer",
    # Config
    "AppIdentity",
    "MergeQueueConfig",
    # Render
    "RenderContext",
    "CycleRenderContext",
    "ActionOutcome",
}


def test_all_expected_dataclasses_are_present() -> None:
    """All expected dataclasses from the plan spec are exported from state.py."""
    actual_names = set(STATE_DATACLASS_NAMES)
    missing = _EXPECTED_DATACLASS_NAMES - actual_names
    assert not missing, (
        f"Expected dataclasses missing from state.py: {sorted(missing)}"
    )


def test_action_union_is_pep604() -> None:
    """Action is a PEP 604 union type (not a class, not an Enum)."""
    from rocm_mq.state import Action

    # Action should be a type alias (Union), not a class
    assert not inspect.isclass(Action), (
        "Action should be a PEP 604 type alias (Activate | Squash | ...), not a class"
    )


def test_deferred_pr_and_partial_pr_state_exported() -> None:
    """DeferredPR and PartialPRState (Q1+Q4 resolutions) are exported from state.py."""
    from rocm_mq.state import DeferredPR, PartialPRState

    assert dataclasses.is_dataclass(DeferredPR)
    assert dataclasses.is_dataclass(PartialPRState)
    assert DeferredPR.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert PartialPRState.__dataclass_params__.frozen  # type: ignore[attr-defined]
