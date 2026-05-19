"""
Minimal RED test: verify _strategies.py and _state_machine_base.py export the required symbols.
These tests fail until those modules exist and export the correct names.

This is a structural TDD test — it verifies the module contract before implementation.
"""

import pytest


def test_strategies_module_exists():
    """_strategies.py must be importable."""
    import tests._strategies  # noqa: F401


def test_strategies_exports_all_required_symbols():
    """_strategies.py must export all seven required strategy functions + CANONICAL_APP."""
    from tests._strategies import (  # noqa: F401
        CANONICAL_APP,
        app_identity_strategy,
        commit_status_creator_strategy,
        label_event_strategy,
        queue_subset_strategy,
        raw_pr_strategy,
        raw_pr_with_forged_activation_status_strategy,
        utc_datetime_strategy,
    )


def test_strategies_does_not_redefine_canonical_merge_queue_config():
    """_strategies.py must NOT define canonical_merge_queue_config (lives in conftest)."""
    import tests._strategies as s

    # Either the attribute does not exist, or it is a re-export from tests.conftest
    if hasattr(s, "canonical_merge_queue_config"):
        assert (
            s.canonical_merge_queue_config.__module__ == "tests.conftest"
        ), "canonical_merge_queue_config must NOT be redefined in tests._strategies"


def test_base_module_exists():
    """_state_machine_base.py must be importable."""
    import tests._state_machine_base  # noqa: F401


def test_base_exports_state_machine_class():
    """_state_machine_base.py must define MergeQueueStateMachineBase."""
    from tests._state_machine_base import MergeQueueStateMachineBase  # noqa: F401


def test_base_has_required_rules():
    """MergeQueueStateMachineBase must define all required rules."""
    from tests._state_machine_base import MergeQueueStateMachineBase

    assert hasattr(MergeQueueStateMachineBase, "setup"), "missing @initialize setup"
    assert hasattr(MergeQueueStateMachineBase, "enqueue"), "missing enqueue @rule"
    assert hasattr(MergeQueueStateMachineBase, "push_new_commit"), "missing push_new_commit @rule"
    assert hasattr(
        MergeQueueStateMachineBase, "simulate_timeline_lag"
    ), "missing simulate_timeline_lag @rule"
    assert hasattr(MergeQueueStateMachineBase, "advance_cycle"), "missing advance_cycle @rule"


def test_base_imports_config_from_conftest_not_strategies():
    """_state_machine_base.py must import canonical_merge_queue_config from tests.conftest."""
    import importlib
    import inspect

    base_src = inspect.getsource(
        importlib.import_module("tests._state_machine_base")
    )
    assert (
        "from tests.conftest import" in base_src
        and "canonical_merge_queue_config" in base_src
    ), "_state_machine_base.py must import canonical_merge_queue_config from tests.conftest"
    assert (
        "from tests._strategies import" not in base_src
        or "canonical_merge_queue_config" not in base_src.split("from tests._strategies import")[1].split("\n")[0]
    ), "_state_machine_base.py must NOT import canonical_merge_queue_config from tests._strategies"
