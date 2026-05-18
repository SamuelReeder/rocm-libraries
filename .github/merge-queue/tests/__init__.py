"""tests — Test package for rocm_mq.

The ``tests`` package is importable so that Plan 04 (invariant suite) and
Plan 05 (worked-example regression) can import shared test utilities:

    from tests.conftest import canonical_merge_queue_config, CANONICAL_APP

This ``__init__.py`` is intentionally empty — all test utilities live in
``tests/conftest.py`` (Plan 03 territory, locked as the single owning home).
"""
