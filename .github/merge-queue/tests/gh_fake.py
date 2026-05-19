"""
tests/gh_fake.py — In-memory GitHub fake for IO-02 / IO-06 contract testing.

Skeleton stage (Task 2 RED): every method raises NotImplementedError. The
GREEN commit fills in the four load-bearing semantic behaviors:

  1. (SHA, context) status overwrite — second write replaces first.
  2. Label add idempotency — adding an existing label is a no-op.
  3. ``remove_label`` 404 — removing an absent label raises a RequestFailed
     with ``.response.status_code == 404``.
  4. ``repos.merge`` 204 vs 201 — already-up-to-date returns 204; merge-fast-
     forward / true merge returns 201 with a fresh SHA in ``.parsed_data.sha``.

Lives in ``tests/`` (not ``src/``) because this is a test substitute, never
imported by production code (T-02-02-04 acceptance).
"""

from __future__ import annotations

# Skeleton stage — full implementation in Task 2 GREEN.


class FakeRepoState:  # pragma: no cover - skeleton stage
    def __init__(self, **_: object) -> None:
        raise NotImplementedError("FakeRepoState skeleton — Task 2 GREEN")


class FakePR:  # pragma: no cover - skeleton stage
    def __init__(self, **_: object) -> None:
        raise NotImplementedError("FakePR skeleton — Task 2 GREEN")


class FakeGitHub:  # pragma: no cover - skeleton stage
    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError("FakeGitHub skeleton — Task 2 GREEN")


class FakeRequestFailed(Exception):  # pragma: no cover - skeleton stage
    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError("FakeRequestFailed skeleton — Task 2 GREEN")
