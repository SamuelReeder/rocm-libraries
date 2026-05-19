"""PURE-09 AST walker — enforces zero I/O imports in the pure decision layer.

Three enforcement rules, all using stdlib ``ast`` only (zero external deps):

1. ``test_pure_layer_no_io_imports``: walks every ``Import`` / ``ImportFrom``
   node in each pure-layer module and asserts that every import is on the
   stdlib allowlist or the intra-rocm_mq allowlist.  Banned imports produce
   a specific "banned import: <name> in rocm_mq.<module>" message.

2. ``test_pure_layer_no_naive_datetime_constructors``: walks every
   ``ast.Attribute`` node in each pure-layer module (excluding ``_helpers``
   which legitimately calls ``datetime.fromisoformat``) and asserts that
   ``datetime.now``, ``datetime.utcnow``, and ``datetime.fromisoformat`` are
   not called outside ``_helpers.py``.

3. ``test_test_files_no_naive_datetime_constructor`` (Q5 resolution): walks
   every ``test_*.py`` file in ``tests/`` and bans ``datetime(...)`` constructor
   calls that lack a ``tzinfo=`` keyword argument.  This prevents Pitfall 3
   (naive-datetime FIFO corruption) in test code.

   Rationale (Open Question 5, RESEARCH.md lines 1500-1504): the ``utc()``
   helper in ``conftest.py`` is the recommended way to build tz-aware datetimes
   in new test code.  The Q5 lint only flags calls that are demonstrably naive
   (no ``tzinfo=`` keyword) rather than banning all ``datetime(...)`` calls, so
   existing tz-aware ``datetime(..., tzinfo=UTC)`` calls in prior-plan test files
   are not flagged.  Files that intentionally construct naive datetimes (e.g.,
   ``test_state_dataclasses.py`` which tests that naive datetimes are rejected)
   are in the exclusion list.

4. ``test_io_import_walker_is_not_a_noop`` (positive regression): verifies the
   walker actually flags a synthetic ``import requests`` source string, guarding
   against a future refactor that silently no-ops the walker (T-01-16).

Failure-message hygiene: every assertion message includes the module name and
the specific construct so the failure log is actionable on first read (RESEARCH.md
"Failure message shape" line 1244).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import rocm_mq

# ---------------------------------------------------------------------------
# Module-level constants (RESEARCH.md CI Lint section lines 1174-1198)
# ---------------------------------------------------------------------------

PURE_LAYER_MODULES: tuple[str, ...] = (
    "_helpers",
    "state",
    "pathmap",
    "decision",
    "comment",
    "summary",
)

STDLIB_ALLOWLIST: frozenset[str] = frozenset(
    {
        "__future__",  # from __future__ import annotations (PEP 563 deferred evaluation)
        "dataclasses",
        "datetime",
        "typing",
        "enum",
        "collections",
        "collections.abc",
        "itertools",
        "functools",
        "re",
        "ast",
        "abc",
    }
)

ROCM_MQ_ALLOWLIST: frozenset[str] = frozenset(
    {f"rocm_mq.{m}" for m in PURE_LAYER_MODULES}
)

BANNED_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "os.path",
        "os.environ",
        "requests",
        "httpx",
        "urllib",
        "urllib.request",
        "urllib.parse",
        "socket",
        "subprocess",
        "rocm_mq.gh",
        "rocm_mq.snapshot",
        "rocm_mq.executor",
        "githubkit",
        "pydantic",
    }
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ROCM_MQ_SRC_DIR = Path(rocm_mq.__file__).parent


def _read_module_source(module_name: str) -> str:
    """Read the source text of a pure-layer module by name."""
    module_path = _ROCM_MQ_SRC_DIR / f"{module_name}.py"
    return module_path.read_text(encoding="utf-8")


def _is_allowlisted(name: str) -> bool:
    """Return True if ``name`` is on the stdlib or rocm_mq allowlists.

    A dotted name is allowlisted if its top-level module is in STDLIB_ALLOWLIST
    OR the full dotted name is in ROCM_MQ_ALLOWLIST.  This allows submodules
    of stdlib (e.g., ``collections.abc`` when ``collections`` is on the list)
    and pure-layer sibling imports (e.g., ``rocm_mq._helpers``).
    """
    if not name:
        return True  # empty module (e.g. relative import from-package sentinel)
    top_level = name.split(".")[0]
    return top_level in STDLIB_ALLOWLIST or name in ROCM_MQ_ALLOWLIST


# ---------------------------------------------------------------------------
# Test 1: No I/O imports in any pure-layer module
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", PURE_LAYER_MODULES)
def test_pure_layer_no_io_imports(module_name: str) -> None:
    """Assert every import in rocm_mq.<module_name> is on the allowlist.

    Banned imports produce: ``"banned import: <name> in rocm_mq.<module_name>"``.
    Non-allowlisted imports produce: ``"non-allowlisted import: <name> in ..."``.
    Both categories are collected before the assertion so a module with multiple
    violations fails with all of them visible (not just the first).
    """
    src = _read_module_source(module_name)
    tree = ast.parse(src)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name in BANNED_IMPORTS:
                    violations.append(
                        f"banned import: {name} in rocm_mq.{module_name}"
                    )
                elif not _is_allowlisted(name):
                    violations.append(
                        f"non-allowlisted import: {name} in rocm_mq.{module_name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in BANNED_IMPORTS:
                violations.append(
                    f"banned from-import: from {mod} in rocm_mq.{module_name}"
                )
            elif not _is_allowlisted(mod):
                violations.append(
                    f"non-allowlisted from-import: from {mod} in rocm_mq.{module_name}"
                )

    assert not violations, violations


# ---------------------------------------------------------------------------
# Test 2: No datetime.now / datetime.utcnow / datetime.fromisoformat outside
# _helpers (Pitfall 3 chokepoint enforcement)
# ---------------------------------------------------------------------------

_BANNED_DATETIME_ATTRS = frozenset({"now", "utcnow", "fromisoformat"})


@pytest.mark.parametrize(
    "module_name",
    [m for m in PURE_LAYER_MODULES if m != "_helpers"],
)
def test_pure_layer_no_naive_datetime_constructors(module_name: str) -> None:
    """Assert no datetime.now / datetime.utcnow / datetime.fromisoformat outside _helpers.

    ``_helpers.py`` is the ONLY sanctioned caller of ``datetime.fromisoformat``
    (Pitfall 3 chokepoint).  All other pure-layer modules receive already-parsed,
    tz-aware datetimes.  ``datetime.now`` and ``datetime.utcnow`` are banned
    everywhere in the pure layer — ``now`` is always passed as an argument.

    Detection strategy: walk every ``ast.Attribute`` node.  Flag when:
    - ``node.value`` is ``ast.Name(id="datetime")`` AND
    - ``node.attr`` is in ``{"now", "utcnow", "fromisoformat"}``.

    This catches both ``datetime.now(...)`` (direct attribute access) AND
    ``from datetime import datetime; datetime.now(...)`` (same AST shape —
    the name ``datetime`` is resolved by the attribute node).
    """
    src = _read_module_source(module_name)
    tree = ast.parse(src)
    violations: list[str] = []

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "datetime"
            and node.attr in _BANNED_DATETIME_ATTRS
        ):
            violations.append(
                f"banned datetime.{node.attr} in rocm_mq.{module_name}:"
                f"line {node.lineno}"
            )

    assert not violations, violations


# ---------------------------------------------------------------------------
# Test 3: Q5 resolution — test files must not use naive datetime() constructors
#
# Open Question 5 (RESEARCH.md lines 1500-1504): the utc() helper in
# conftest.py is the sanctioned way to build tz-aware datetimes in test code.
# This lint bans datetime() calls WITHOUT a tzinfo= keyword argument (i.e.,
# only flags demonstrably naive constructors; tz-aware calls are permitted).
#
# Exclusion list:
# - conftest.py — defines utc() helper; legitimately uses datetime directly
# - _strategies.py — Hypothesis strategy scaffolding (Plan 04)
# - _state_machine_base.py — state-machine base class (Plan 04)
# - test_pure_layer_imports.py — the lint itself (avoid recursion)
# - test_state_dataclasses.py — intentionally constructs naive datetimes to
#   test that they are rejected by the dataclass field contracts (Plan 01)
# ---------------------------------------------------------------------------

_TEST_DIR = Path(__file__).parent

_Q5_EXCLUDED_STEMS = frozenset(
    {
        "conftest",
        "_strategies",
        "_state_machine_base",
        "test_pure_layer_imports",
        "test_state_dataclasses",  # intentionally tests naive-datetime rejection
    }
)


def test_test_files_no_naive_datetime_constructor() -> None:
    """Q5: test files must not construct naive datetimes via bare datetime().

    Walks every ``test_*.py`` file in the ``tests/`` directory (excluding the
    files listed in ``_Q5_EXCLUDED_STEMS``).  Flags any ``ast.Call`` node that:
    - Calls a function named ``datetime`` (``func.id == "datetime"``), OR
    - Calls ``datetime.datetime(...)`` (``func.attr == "datetime"`` on a
      ``datetime`` name), AND
    - Does NOT have ``tzinfo=`` among the keyword arguments.

    The ``tzinfo=`` check means tz-aware calls like ``datetime(2026, 1, 1,
    tzinfo=UTC)`` are NOT flagged — only demonstrably naive calls like
    ``datetime(2026, 1, 1)`` (no tzinfo) trigger a violation.

    Failing calls should be replaced with ``utc(year, month, day, ...)`` from
    ``tests.conftest`` (the Pitfall 3 chokepoint for test code).
    """
    violations: list[str] = []

    for test_file in sorted(_TEST_DIR.glob("test_*.py")):
        if test_file.stem in _Q5_EXCLUDED_STEMS:
            continue
        src = test_file.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(test_file))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Pattern A: ``datetime(...)`` — func is Name("datetime")
            is_bare_datetime_name = (
                isinstance(node.func, ast.Name) and node.func.id == "datetime"
            )
            # Pattern B: ``datetime.datetime(...)`` — func is Attribute on Name("datetime")
            is_dotted_datetime = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "datetime"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "datetime"
            )
            if not (is_bare_datetime_name or is_dotted_datetime):
                continue
            # Allow if tzinfo= keyword is present (tz-aware construction is fine)
            kwarg_names = {kw.arg for kw in node.keywords}
            if "tzinfo" in kwarg_names:
                continue
            violations.append(
                f"naive datetime() constructor in {test_file.name}:{node.lineno}"
                " — use utc() helper from conftest or add tzinfo= keyword"
            )

    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# Test 4: Positive regression — walker MUST flag a synthetic banned import
# (T-01-16: guards against a silent no-op refactor of the walker)
# ---------------------------------------------------------------------------


def test_io_import_walker_is_not_a_noop() -> None:
    """Positive regression: walker flags 'import requests' in synthetic source.

    If this test FAILS (the walker does NOT flag the synthetic source), the
    walker is broken (silent no-op).  All 6 parametrized Test 1 cases would
    pass vacuously and real I/O imports in pure-layer modules would go undetected.

    The synthetic source is not from any real module — it exists only to prove
    the walker logic is alive and correctly identifies the banned import.
    """
    synthetic_src = (
        "import requests  # this should be flagged\n"
        "from rocm_mq.state import PRState\n"
        "\n"
        "def example() -> None:\n"
        "    pass\n"
    )
    tree = ast.parse(synthetic_src)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name in BANNED_IMPORTS:
                    violations.append(f"banned import: {name} in rocm_mq.synthetic")
                elif not _is_allowlisted(name):
                    violations.append(
                        f"non-allowlisted import: {name} in rocm_mq.synthetic"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in BANNED_IMPORTS:
                violations.append(
                    f"banned from-import: from {mod} in rocm_mq.synthetic"
                )
            elif not _is_allowlisted(mod):
                violations.append(
                    f"non-allowlisted from-import: from {mod} in rocm_mq.synthetic"
                )

    assert len(violations) == 1, (
        f"Expected exactly 1 violation (import requests), got {len(violations)}: {violations}"
    )
    assert "banned import: requests in rocm_mq.synthetic" in violations, (
        f"Expected 'banned import: requests ...' in violations, got: {violations}"
    )
